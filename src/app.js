const { GAME_CONFIG } = require("./config");
const { AdManager } = require("./ad-manager");
const {
  loadProfile,
  saveProfile,
  refreshDaily,
  updateProfileAfterRound,
  addCoins,
} = require("./storage");
const {
  loadAnalytics,
  saveAnalytics,
  markSessionStart,
  trackRoundStart,
  trackRoundSettled,
  trackAdRequest,
  trackAdComplete,
  getAnalyticsSnapshot,
} = require("./analytics");
const { LeaderboardManager } = require("./leaderboard");

class MiniGameApp {
  constructor() {
    const system = wx.getSystemInfoSync();
    this.width = system.windowWidth;
    this.height = system.windowHeight;
    this.pixelRatio = system.pixelRatio || 1;

    this.canvas = wx.createCanvas();
    this.ctx = this.canvas.getContext("2d");
    this.canvas.width = this.width * this.pixelRatio;
    this.canvas.height = this.height * this.pixelRatio;
    this.ctx.scale(this.pixelRatio, this.pixelRatio);

    this.adManager = new AdManager();
    this.adManager.init(this.width, this.height);

    this.profile = refreshDaily(loadProfile(), GAME_CONFIG.retention);
    saveProfile(this.profile);

    this.analytics = markSessionStart(loadAnalytics());
    saveAnalytics(this.analytics);
    this.analyticsSnapshot = getAnalyticsSnapshot(this.analytics);

    this.leaderboard = new LeaderboardManager(GAME_CONFIG.leaderboard);
    this.leaderboardEntries = this.leaderboard.getCachedEntries();
    this.leaderboardSource = this.leaderboardEntries.length > 0 ? "cache" : "fallback";
    this.leaderboardLoading = false;

    this.state = "menu";
    this.lastTimestamp = 0;
    this.awaitingReviveAd = false;
    this.awaitingDoubleAd = false;
    this.roundMessage = "";
    this.roundCoinGain = 0;
    this.dailyBonusGain = 0;
    this.roundFinalized = false;
    this.doubleRewardClaimed = false;
    this.effectState = {
      shieldCharges: 0,
      magnetUntil: 0,
      slowUntil: 0,
    };

    this.player = {
      x: this.width / 2,
      y: this.height - 86,
      w: GAME_CONFIG.player.width,
      h: GAME_CONFIG.player.height,
    };

    this.loop = this.loop.bind(this);
    this.initTouchEvents();
    this.resetRound();
    this.computeButtons();
    this.showBannerTracked();
    this.refreshLeaderboard(false);
    requestAnimationFrame(this.loop);
  }

  initTouchEvents() {
    wx.onTouchStart((event) => {
      const touch = (event.touches && event.touches[0]) || event.changedTouches[0];
      if (!touch) {
        return;
      }
      this.handleTouchStart(touch.clientX, touch.clientY);
    });

    wx.onTouchMove((event) => {
      if (this.state !== "playing") {
        return;
      }
      const touch = (event.touches && event.touches[0]) || event.changedTouches[0];
      if (!touch) {
        return;
      }
      this.targetX = touch.clientX;
    });
  }

  computeButtons() {
    this.startButton = {
      x: this.width / 2 - 112,
      y: this.height * 0.69,
      w: 224,
      h: 52,
    };
    this.rankRefreshButton = {
      x: this.width / 2 - 112,
      y: this.height * 0.78,
      w: 224,
      h: 46,
    };
    this.settleButton = {
      x: this.width / 2 - 112,
      y: this.height * 0.63,
      w: 224,
      h: 52,
    };
    this.reviveButton = {
      x: this.width / 2 - 112,
      y: this.height * 0.72,
      w: 224,
      h: 52,
    };
    this.doubleRewardButton = {
      x: this.width / 2 - 112,
      y: this.height * 0.63,
      w: 224,
      h: 52,
    };
    this.retryButton = {
      x: this.width / 2 - 112,
      y: this.height * 0.72,
      w: 224,
      h: 52,
    };
    this.overRankButton = {
      x: this.width / 2 - 112,
      y: this.height * 0.81,
      w: 224,
      h: 46,
    };
  }

  resetRound() {
    this.score = 0;
    this.level = 1;
    this.elapsedMs = 0;
    this.spawnTimer = 0;
    this.comboCount = 0;
    this.comboExpireAt = 0;
    this.items = [];
    this.revived = false;
    this.invincibleUntil = 0;
    this.targetX = this.player.x;
    this.roundCoinGain = 0;
    this.dailyBonusGain = 0;
    this.roundFinalized = false;
    this.doubleRewardClaimed = false;
    this.awaitingReviveAd = false;
    this.awaitingDoubleAd = false;
    this.roundMessage = "";
    this.effectState = {
      shieldCharges: 0,
      magnetUntil: 0,
      slowUntil: 0,
    };
    this.message = "按住并移动手指，接金币躲炸弹";
    this.messageUntil = Date.now() + 2500;
  }

  startRound() {
    this.resetRound();
    this.state = "playing";
    this.adManager.hideBanner();

    this.analytics = trackRoundStart(this.analytics);
    this.persistAnalytics();
  }

  handleTouchStart(x, y) {
    if (this.state === "menu") {
      if (this.isInButton(x, y, this.startButton)) {
        this.startRound();
        return;
      }
      if (this.isInButton(x, y, this.rankRefreshButton)) {
        this.refreshLeaderboard(true);
      }
      return;
    }

    if (this.state === "playing") {
      this.targetX = x;
      return;
    }

    if (this.state !== "gameover" || this.awaitingReviveAd || this.awaitingDoubleAd) {
      return;
    }

    if (!this.roundFinalized) {
      if (!this.revived && this.isInButton(x, y, this.reviveButton)) {
        this.tryReviveByAd();
        return;
      }
      if (this.isInButton(x, y, this.settleButton)) {
        this.finalizeRound();
      }
      return;
    }

    if (
      !this.doubleRewardClaimed &&
      this.isInButton(x, y, this.doubleRewardButton)
    ) {
      this.claimDoubleRewardByAd();
      return;
    }

    if (this.isInButton(x, y, this.retryButton)) {
      this.startRound();
      return;
    }

    if (this.isInButton(x, y, this.overRankButton)) {
      this.refreshLeaderboard(true);
    }
  }

  isInButton(x, y, btn) {
    return x >= btn.x && x <= btn.x + btn.w && y >= btn.y && y <= btn.y + btn.h;
  }

  getAdRevenue(adType) {
    return GAME_CONFIG.analytics.adRevenuePerImpression[adType] || 0;
  }

  persistAnalytics() {
    saveAnalytics(this.analytics);
    this.analyticsSnapshot = getAnalyticsSnapshot(this.analytics);
  }

  recordAdRequest(adType) {
    this.analytics = trackAdRequest(this.analytics, adType);
    this.persistAnalytics();
  }

  recordAdComplete(adType) {
    this.analytics = trackAdComplete(
      this.analytics,
      adType,
      this.getAdRevenue(adType)
    );
    this.persistAnalytics();
  }

  showBannerTracked() {
    this.recordAdRequest("banner");
    this.adManager.showBanner().then((shown) => {
      if (shown) {
        this.recordAdComplete("banner");
      }
    });
  }

  showInterstitialTracked() {
    this.recordAdRequest("interstitial");
    this.adManager.showInterstitial().then((shown) => {
      if (shown) {
        this.recordAdComplete("interstitial");
      }
    });
  }

  refreshLeaderboard(showToast) {
    if (this.leaderboardLoading) {
      return;
    }
    this.leaderboardLoading = true;
    this.leaderboard.refresh(this.profile.bestScore).then((res) => {
      this.leaderboardLoading = false;
      this.leaderboardEntries = res.entries;
      this.leaderboardSource = res.source;
      if (showToast) {
        this.toast(
          res.source === "friend-cloud" ? "好友榜已刷新" : "已刷新(本地榜)"
        );
      }
    });
  }

  tryReviveByAd() {
    this.awaitingReviveAd = true;
    this.recordAdRequest("rewarded");
    this.adManager.showRewardedVideo().then((completed) => {
      this.awaitingReviveAd = false;
      if (!completed) {
        this.toast("广告未完整播放，复活失败");
        return;
      }

      this.recordAdComplete("rewarded");
      this.revived = true;
      this.state = "playing";
      this.invincibleUntil = Date.now() + GAME_CONFIG.progression.invincibleAfterReviveMs;
      this.items = this.items.filter((item) => item.type !== "bomb");
      this.message = "复活成功：短暂无敌";
      this.messageUntil = Date.now() + 1800;
      this.adManager.hideBanner();
    });
  }

  claimDoubleRewardByAd() {
    this.awaitingDoubleAd = true;
    this.recordAdRequest("rewarded");
    this.adManager.showRewardedVideo().then((completed) => {
      this.awaitingDoubleAd = false;
      if (!completed) {
        this.toast("广告未完整播放，奖励不生效");
        return;
      }

      this.recordAdComplete("rewarded");

      const extra = Math.floor(
        this.roundCoinGain * (GAME_CONFIG.economy.doubleRewardMultiplier - 1)
      );
      this.profile = addCoins(this.profile, extra);
      saveProfile(this.profile);

      this.roundCoinGain += extra;
      this.doubleRewardClaimed = true;
      this.roundMessage = `双倍奖励生效：额外 +${extra} 金币`;

      this.analytics.totalCoinGain += extra;
      this.persistAnalytics();
      this.toast("双倍奖励已到账");
    });
  }

  toast(title) {
    if (!wx.showToast) {
      return;
    }
    wx.showToast({
      title,
      icon: "none",
      duration: 1200,
    });
  }

  onRoundFailed() {
    this.state = "gameover";
    this.showBannerTracked();

    if (this.revived) {
      this.finalizeRound();
      return;
    }

    this.roundMessage = "可看广告复活1次，或直接结算";
  }

  finalizeRound() {
    if (this.roundFinalized) {
      return;
    }

    const result = updateProfileAfterRound(
      this.profile,
      this.score,
      GAME_CONFIG.retention,
      GAME_CONFIG.economy
    );

    this.profile = result.profile;
    this.roundCoinGain = result.roundCoinGain;
    this.dailyBonusGain = result.dailyBonus;
    this.roundFinalized = true;
    saveProfile(this.profile);

    this.leaderboard.submitBestScore(this.profile.bestScore);
    this.refreshLeaderboard(false);

    this.analytics = trackRoundSettled(
      this.analytics,
      this.score,
      this.roundCoinGain
    );
    this.persistAnalytics();

    if (this.dailyBonusGain > 0) {
      this.roundMessage = `今日目标达成，额外 +${this.dailyBonusGain} 金币`;
    } else {
      this.roundMessage = `本局结算 +${this.roundCoinGain} 金币`;
    }

    if (
      this.profile.totalRounds > 0 &&
      this.profile.totalRounds % GAME_CONFIG.retention.interstitialGapRounds === 0
    ) {
      this.showInterstitialTracked();
    }
  }

  loop(timestamp) {
    if (!this.lastTimestamp) {
      this.lastTimestamp = timestamp;
    }

    const dtMs = Math.min(48, timestamp - this.lastTimestamp || 16);
    this.lastTimestamp = timestamp;

    this.update(dtMs);
    this.render();

    requestAnimationFrame(this.loop);
  }

  update(dtMs) {
    if (this.state !== "playing") {
      return;
    }

    const now = Date.now();
    const smooth = 1 - Math.pow(1 - GAME_CONFIG.player.speedFollow, dtMs / 16.67);
    this.player.x += (this.targetX - this.player.x) * smooth;
    this.player.x = this.clamp(
      this.player.x,
      this.player.w / 2,
      this.width - this.player.w / 2
    );

    this.elapsedMs += dtMs;
    this.level =
      1 + Math.floor(this.elapsedMs / GAME_CONFIG.progression.levelDurationMs);

    const spawnInterval = Math.max(
      GAME_CONFIG.item.minSpawnInterval,
      GAME_CONFIG.item.spawnInterval -
        (this.level - 1) * GAME_CONFIG.item.spawnIntervalGainPerLevel
    );
    this.spawnTimer += dtMs;

    while (this.spawnTimer >= spawnInterval) {
      this.spawnTimer -= spawnInterval;
      this.spawnItem();
    }

    if (now > this.comboExpireAt) {
      this.comboCount = 0;
    }

    const slowScale =
      now < this.effectState.slowUntil ? GAME_CONFIG.props.slow.speedScale : 1;
    const dtSec = dtMs / 1000;

    for (let i = this.items.length - 1; i >= 0; i -= 1) {
      const item = this.items[i];

      if (item.type === "coin" && now < this.effectState.magnetUntil) {
        this.applyMagnet(item, dtSec);
      }

      item.y += item.speed * slowScale * dtSec;

      if (item.y - item.radius > this.height) {
        this.items.splice(i, 1);
        continue;
      }

      if (!this.hitPlayer(item)) {
        continue;
      }

      this.items.splice(i, 1);
      if (item.type === "coin") {
        this.onCoinCollect();
        continue;
      }

      if (item.type === "prop") {
        this.onPropCollect(item.propType);
        continue;
      }

      if (now <= this.invincibleUntil) {
        continue;
      }

      if (this.effectState.shieldCharges > 0) {
        this.effectState.shieldCharges -= 1;
        this.message = "护盾挡住了炸弹";
        this.messageUntil = now + 900;
        continue;
      }

      this.onRoundFailed();
      return;
    }
  }

  applyMagnet(item, dtSec) {
    const range = GAME_CONFIG.props.magnet.attractRange;
    const dx = this.player.x - item.x;
    if (Math.abs(dx) > range) {
      return;
    }
    const step = GAME_CONFIG.props.magnet.attractSpeed * dtSec;
    if (Math.abs(dx) <= step) {
      item.x = this.player.x;
      return;
    }
    item.x += dx > 0 ? step : -step;
  }

  spawnItem() {
    const radius = GAME_CONFIG.item.radius;
    const baseSpeed =
      GAME_CONFIG.item.baseSpeed +
      (this.level - 1) * GAME_CONFIG.item.levelSpeedGain +
      Math.random() * GAME_CONFIG.item.speedRandom;

    if (Math.random() < GAME_CONFIG.item.propRate) {
      this.items.push({
        type: "prop",
        propType: this.pickPropType(),
        x: GAME_CONFIG.item.propRadius + Math.random() * (this.width - GAME_CONFIG.item.propRadius * 2),
        y: -GAME_CONFIG.item.propRadius,
        radius: GAME_CONFIG.item.propRadius,
        speed: baseSpeed * 0.78,
      });
      return;
    }

    const bombRate = Math.min(
      GAME_CONFIG.item.maxBombRate,
      GAME_CONFIG.item.baseBombRate +
        (this.level - 1) * GAME_CONFIG.item.bombRateGainPerLevel
    );

    const isBomb = Math.random() < bombRate;
    this.items.push({
      type: isBomb ? "bomb" : "coin",
      x: radius + Math.random() * (this.width - radius * 2),
      y: -radius,
      radius,
      speed: baseSpeed,
    });
  }

  pickPropType() {
    const candidates = ["shield", "magnet", "slow"];
    return candidates[Math.floor(Math.random() * candidates.length)];
  }

  onCoinCollect() {
    const now = Date.now();
    if (now <= this.comboExpireAt) {
      this.comboCount += 1;
    } else {
      this.comboCount = 1;
    }

    this.comboExpireAt = now + GAME_CONFIG.progression.comboWindowMs;
    const comboMultiplier = this.clamp(
      1 + Math.floor((this.comboCount - 1) / 4),
      1,
      GAME_CONFIG.progression.maxComboMultiplier
    );
    this.score += comboMultiplier;

    if (comboMultiplier >= 2) {
      this.message = `连击 x${comboMultiplier}`;
      this.messageUntil = now + 750;
    }
  }

  onPropCollect(propType) {
    const now = Date.now();
    if (propType === "shield") {
      this.effectState.shieldCharges += GAME_CONFIG.props.shield.bombBlockCount;
      this.message = `护盾 +${GAME_CONFIG.props.shield.bombBlockCount}`;
      this.messageUntil = now + 900;
      return;
    }

    if (propType === "magnet") {
      const start = Math.max(this.effectState.magnetUntil, now);
      this.effectState.magnetUntil = start + GAME_CONFIG.props.magnet.durationMs;
      this.message = "磁铁生效";
      this.messageUntil = now + 900;
      return;
    }

    const start = Math.max(this.effectState.slowUntil, now);
    this.effectState.slowUntil = start + GAME_CONFIG.props.slow.durationMs;
    this.message = "全场减速";
    this.messageUntil = now + 900;
  }

  hitPlayer(item) {
    const left = this.player.x - this.player.w / 2;
    const right = this.player.x + this.player.w / 2;
    const top = this.player.y - this.player.h / 2;
    const bottom = this.player.y + this.player.h / 2;
    return (
      item.x + item.radius >= left &&
      item.x - item.radius <= right &&
      item.y + item.radius >= top &&
      item.y - item.radius <= bottom
    );
  }

  clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  render() {
    const { ctx } = this;
    const palette = GAME_CONFIG.ui;

    ctx.clearRect(0, 0, this.width, this.height);
    ctx.fillStyle = palette.background;
    ctx.fillRect(0, 0, this.width, this.height);

    this.drawHud();
    this.drawItems();
    this.drawPlayer();
    this.drawMessage();

    if (this.state === "menu") {
      this.drawMenu();
    } else if (this.state === "gameover") {
      this.drawGameOver();
    }
  }

  drawHud() {
    const { ctx } = this;
    const palette = GAME_CONFIG.ui;
    const now = Date.now();

    ctx.fillStyle = palette.panel;
    ctx.fillRect(12, 12, this.width - 24, 86);

    ctx.fillStyle = palette.textPrimary;
    ctx.font = "bold 20px sans-serif";
    ctx.fillText(`分数 ${this.score}`, 24, 40);
    ctx.fillText(`Lv.${this.level}`, this.width - 108, 40);

    ctx.fillStyle = palette.textMuted;
    ctx.font = "15px sans-serif";
    const comboMultiplier = this.clamp(
      1 + Math.floor((Math.max(this.comboCount, 1) - 1) / 4),
      1,
      GAME_CONFIG.progression.maxComboMultiplier
    );
    ctx.fillText(`连击倍率 x${comboMultiplier}`, 24, 64);

    const effects = [];
    if (this.effectState.shieldCharges > 0) {
      effects.push(`盾x${this.effectState.shieldCharges}`);
    }
    if (now < this.effectState.magnetUntil) {
      effects.push(`磁:${Math.ceil((this.effectState.magnetUntil - now) / 1000)}s`);
    }
    if (now < this.effectState.slowUntil) {
      effects.push(`缓:${Math.ceil((this.effectState.slowUntil - now) / 1000)}s`);
    }
    ctx.fillText(
      effects.length > 0 ? `道具 ${effects.join(" | ")}` : "道具 暂无",
      24,
      86
    );
  }

  drawItems() {
    const { ctx } = this;
    for (const item of this.items) {
      if (item.type === "prop") {
        const propConfig = GAME_CONFIG.props[item.propType];
        ctx.beginPath();
        ctx.fillStyle = propConfig.color;
        ctx.arc(item.x, item.y, item.radius, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "#e2e8f0";
        ctx.font = "bold 13px sans-serif";
        ctx.fillText(propConfig.icon, item.x - 6, item.y + 5);
        continue;
      }

      ctx.beginPath();
      ctx.fillStyle =
        item.type === "coin" ? GAME_CONFIG.ui.accent : GAME_CONFIG.ui.danger;
      ctx.arc(item.x, item.y, item.radius, 0, Math.PI * 2);
      ctx.fill();

      if (item.type === "coin") {
        ctx.fillStyle = "#92400e";
        ctx.font = "bold 14px sans-serif";
        ctx.fillText("$", item.x - 4, item.y + 5);
      } else {
        ctx.fillStyle = "#fee2e2";
        ctx.font = "bold 14px sans-serif";
        ctx.fillText("!", item.x - 2, item.y + 5);
      }
    }
  }

  drawPlayer() {
    const { ctx } = this;
    const x = this.player.x - this.player.w / 2;
    const y = this.player.y - this.player.h / 2;

    ctx.fillStyle = "#38bdf8";
    ctx.fillRect(x, y, this.player.w, this.player.h);

    if (Date.now() <= this.invincibleUntil) {
      ctx.strokeStyle = "#22d3ee";
      ctx.lineWidth = 3;
      ctx.strokeRect(x - 3, y - 3, this.player.w + 6, this.player.h + 6);
    }

    ctx.fillStyle = GAME_CONFIG.ui.textPrimary;
    ctx.font = "12px sans-serif";
    ctx.fillText("接金币盘", x + 8, y + 14);
  }

  drawMessage() {
    if (!this.message || Date.now() > this.messageUntil) {
      return;
    }
    const { ctx } = this;
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 18px sans-serif";
    const w = ctx.measureText(this.message).width;
    ctx.fillText(this.message, (this.width - w) / 2, this.height * 0.23);
  }

  drawMenu() {
    const { ctx } = this;
    const palette = GAME_CONFIG.ui;
    const panelX = 18;
    const panelY = this.height * 0.08;
    const panelW = this.width - 36;
    const panelH = this.height * 0.84;

    ctx.fillStyle = "rgba(15, 23, 42, 0.86)";
    ctx.fillRect(panelX, panelY, panelW, panelH);

    ctx.fillStyle = palette.textPrimary;
    ctx.font = "bold 30px sans-serif";
    const title = GAME_CONFIG.ui.title;
    ctx.fillText(title, (this.width - ctx.measureText(title).width) / 2, panelY + 46);

    ctx.font = "16px sans-serif";
    ctx.fillText("好友榜 + 道具系统 + 广告双倍奖励", panelX + 20, panelY + 82);
    ctx.fillText("复活和双倍都走激励视频，便于提高变现", panelX + 20, panelY + 106);

    ctx.fillStyle = palette.textMuted;
    ctx.fillText(`历史最高：${this.profile.bestScore}`, panelX + 20, panelY + 136);
    ctx.fillText(`总金币：${this.profile.totalCoins}`, panelX + 20, panelY + 160);
    ctx.fillText(`连续打卡：${this.profile.streakDays} 天`, panelX + 20, panelY + 184);
    ctx.fillText(
      `今日目标：${this.profile.dailyProgress}/${this.profile.dailyTarget}`,
      panelX + 20,
      panelY + 208
    );

    this.drawLeaderboardCard(panelX + 16, panelY + 224, panelW - 32, 132, 4);
    this.drawAnalyticsCard(panelX + 16, panelY + 366, panelW - 32, 102);

    this.drawButton(this.startButton, "开始闯关", "#f59e0b");
    this.drawButton(this.rankRefreshButton, "刷新好友榜", "#22c55e");
  }

  drawGameOver() {
    const { ctx } = this;
    const palette = GAME_CONFIG.ui;

    ctx.fillStyle = "rgba(2, 6, 23, 0.78)";
    ctx.fillRect(0, 0, this.width, this.height);

    ctx.fillStyle = palette.textPrimary;
    ctx.font = "bold 34px sans-serif";
    const over = "本局结束";
    ctx.fillText(over, (this.width - ctx.measureText(over).width) / 2, this.height * 0.25);

    ctx.font = "20px sans-serif";
    const scoreText = `得分 ${this.score} | 结算金币 ${this.roundCoinGain}`;
    ctx.fillText(
      scoreText,
      (this.width - ctx.measureText(scoreText).width) / 2,
      this.height * 0.32
    );

    ctx.fillStyle = palette.textMuted;
    ctx.font = "16px sans-serif";
    const profileText = `最高 ${this.profile.bestScore} | 总金币 ${this.profile.totalCoins}`;
    ctx.fillText(
      profileText,
      (this.width - ctx.measureText(profileText).width) / 2,
      this.height * 0.36
    );

    if (this.roundMessage) {
      ctx.fillStyle = "#86efac";
      ctx.fillText(
        this.roundMessage,
        (this.width - ctx.measureText(this.roundMessage).width) / 2,
        this.height * 0.4
      );
    }

    this.drawLeaderboardCard(18, this.height * 0.43, this.width - 36, 132, 4);

    if (!this.roundFinalized) {
      this.drawButton(this.settleButton, "直接结算", "#f59e0b");
      if (!this.revived) {
        this.drawButton(
          this.reviveButton,
          this.awaitingReviveAd ? "广告加载中..." : "看广告复活",
          "#22c55e"
        );
      }
      return;
    }

    this.drawButton(
      this.doubleRewardButton,
      this.awaitingDoubleAd
        ? "广告加载中..."
        : this.doubleRewardClaimed
        ? "双倍奖励已领取"
        : "看广告双倍奖励",
      this.doubleRewardClaimed ? "#475569" : "#22c55e"
    );
    this.drawButton(this.retryButton, "再来一局", "#f59e0b");
    this.drawButton(this.overRankButton, "刷新好友榜", "#1d4ed8");
  }

  drawLeaderboardCard(x, y, w, h, rows) {
    const { ctx } = this;
    ctx.fillStyle = "rgba(30, 41, 59, 0.95)";
    ctx.fillRect(x, y, w, h);

    ctx.fillStyle = "#f8fafc";
    ctx.font = "bold 16px sans-serif";
    const sourceLabel =
      this.leaderboardSource === "friend-cloud" ? "好友榜" : "本地模拟好友榜";
    ctx.fillText(sourceLabel, x + 12, y + 24);

    ctx.fillStyle = "#94a3b8";
    ctx.font = "12px sans-serif";
    if (this.leaderboardLoading) {
      ctx.fillText("刷新中...", x + w - 68, y + 24);
    }

    const entries = this.leaderboardEntries.slice(0, rows);
    ctx.font = "14px sans-serif";
    entries.forEach((entry, index) => {
      const rowY = y + 48 + index * 20;
      ctx.fillStyle = entry.isSelf ? "#fde68a" : "#e2e8f0";
      const name = entry.nickname.length > 7 ? `${entry.nickname.slice(0, 7)}…` : entry.nickname;
      ctx.fillText(`${entry.rank}. ${name}`, x + 12, rowY);

      const scoreText = `${entry.score}`;
      const textW = ctx.measureText(scoreText).width;
      ctx.fillText(scoreText, x + w - textW - 12, rowY);
    });
  }

  drawAnalyticsCard(x, y, w, h) {
    const { ctx } = this;
    const stats = this.analyticsSnapshot;
    ctx.fillStyle = "rgba(15, 23, 42, 0.95)";
    ctx.fillRect(x, y, w, h);

    ctx.fillStyle = "#f8fafc";
    ctx.font = "bold 16px sans-serif";
    ctx.fillText("埋点看板", x + 12, y + 24);

    ctx.fillStyle = "#cbd5e1";
    ctx.font = "13px sans-serif";
    ctx.fillText(
      `留存(D1): ${stats.d1Retained ? "已达成" : "未达成"}  | 活跃天: ${stats.activeDays}`,
      x + 12,
      y + 48
    );
    ctx.fillText(
      `完成率: ${stats.completionRate}%  | 激励完成: ${stats.rewardCompletionRate}%`,
      x + 12,
      y + 68
    );
    ctx.fillText(`ARPU(估算): ${stats.arpu} 元`, x + 12, y + 88);
  }

  drawButton(rect, text, color) {
    const { ctx } = this;
    ctx.fillStyle = color || "#f59e0b";
    ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
    ctx.fillStyle = "#0f172a";
    ctx.font = "bold 20px sans-serif";
    const w = ctx.measureText(text).width;
    ctx.fillText(text, rect.x + (rect.w - w) / 2, rect.y + 33);
  }
}

module.exports = {
  MiniGameApp,
};
