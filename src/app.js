const { GAME_CONFIG } = require("./config");
const { AdManager } = require("./ad-manager");
const { MonetizationBridge } = require("./monetization");
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
  trackRewardGrant,
  getAnalyticsSnapshot,
} = require("./analytics");
const { LeaderboardManager } = require("./leaderboard");
const { WorldLeaderboardManager } = require("./world-leaderboard");

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
    this.monetization = new MonetizationBridge(this.adManager, GAME_CONFIG.monetization);
    this.monetization.init(this.width, this.height);

    this.profile = refreshDaily(loadProfile(), GAME_CONFIG.retention);
    saveProfile(this.profile);

    this.analytics = markSessionStart(loadAnalytics());
    saveAnalytics(this.analytics);
    this.analyticsSnapshot = getAnalyticsSnapshot(this.analytics);

    this.friendBoardManager = new LeaderboardManager(GAME_CONFIG.leaderboard);
    this.worldBoardManager = new WorldLeaderboardManager(GAME_CONFIG.worldLeaderboard);
    this.rankView = "friend";
    this.rankBoards = {
      friend: {
        entries: this.friendBoardManager.getCachedEntries(),
        source: "cache",
        loading: false,
      },
      world: {
        entries: this.worldBoardManager.getCachedEntries(),
        source: "cache",
        loading: false,
      },
    };

    this.state = "menu";
    this.lastTimestamp = 0;
    this.awaitingReviveReward = false;
    this.awaitingDoubleReward = false;
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

    this.backgroundParticles = this.createBackgroundParticles(32);

    this.player = {
      x: this.width / 2,
      y: this.height - 88,
      w: GAME_CONFIG.player.width,
      h: GAME_CONFIG.player.height,
    };

    this.loop = this.loop.bind(this);
    this.initTouchEvents();
    this.computeButtons();
    this.resetRound();
    this.showBannerTracked();
    this.refreshBoard("friend", false);
    this.refreshBoard("world", false);
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
    this.startButton = this.buttonRect(0.71, 224, 52);
    this.menuSwitchRankButton = this.buttonRect(0.79, 224, 44);
    this.menuRefreshRankButton = this.buttonRect(0.85, 224, 44);

    this.settleButton = this.buttonRect(0.62, 224, 50);
    this.reviveButton = this.buttonRect(0.70, 224, 50);
    this.doubleRewardButton = this.buttonRect(0.61, 224, 50);
    this.retryButton = this.buttonRect(0.69, 224, 50);
    this.overSwitchRankButton = this.buttonRect(0.77, 224, 44);
    this.overRefreshRankButton = this.buttonRect(0.84, 224, 44);
  }

  buttonRect(yRate, width, height) {
    return {
      x: this.width / 2 - width / 2,
      y: this.height * yRate,
      w: width,
      h: height,
    };
  }

  createBackgroundParticles(count) {
    const particles = [];
    for (let i = 0; i < count; i += 1) {
      particles.push({
        x: Math.random() * this.width,
        y: Math.random() * this.height,
        r: 1 + Math.random() * 2,
        speed: 10 + Math.random() * 18,
        alpha: 0.3 + Math.random() * 0.4,
      });
    }
    return particles;
  }

  resetRound() {
    this.score = 0;
    this.level = 1;
    this.elapsedMs = 0;
    this.spawnTimer = 0;
    this.comboCount = 0;
    this.comboExpireAt = 0;
    this.feverUntil = 0;
    this.items = [];
    this.revived = false;
    this.invincibleUntil = 0;
    this.targetX = this.player.x;
    this.roundCoinGain = 0;
    this.dailyBonusGain = 0;
    this.roundFinalized = false;
    this.doubleRewardClaimed = false;
    this.awaitingReviveReward = false;
    this.awaitingDoubleReward = false;
    this.roundMessage = "";
    this.effectState = {
      shieldCharges: 0,
      magnetUntil: 0,
      slowUntil: 0,
    };

    this.roundMission = this.generateMission();
    this.missionProgress = 0;
    this.missionCompleted = false;
    this.missionBonusCoins = this.roundMission.rewardCoins;

    this.message = "按住并左右移动接金币，连击能进入狂热";
    this.messageUntil = Date.now() + 2400;
  }

  generateMission() {
    const missionTemplates = [
      {
        type: "coin",
        target: 24 + Math.floor(Math.random() * 20),
        rewardCoins: this.randomMissionReward(),
      },
      {
        type: "prop",
        target: 3 + Math.floor(Math.random() * 2),
        rewardCoins: this.randomMissionReward(),
      },
      {
        type: "survival",
        target: 30 + Math.floor(Math.random() * 20),
        rewardCoins: this.randomMissionReward(),
      },
    ];

    const mission = missionTemplates[Math.floor(Math.random() * missionTemplates.length)];
    mission.label = this.buildMissionLabel(mission);
    return mission;
  }

  randomMissionReward() {
    const min = GAME_CONFIG.economy.missionBonusMin;
    const max = GAME_CONFIG.economy.missionBonusMax;
    return min + Math.floor(Math.random() * (max - min + 1));
  }

  buildMissionLabel(mission) {
    if (mission.type === "coin") {
      return `任务: 接到 ${mission.target} 枚金币`;
    }
    if (mission.type === "prop") {
      return `任务: 吃到 ${mission.target} 个道具`;
    }
    return `任务: 存活 ${mission.target} 秒`;
  }

  startRound() {
    this.resetRound();
    this.state = "playing";
    this.monetization.hideBanner();

    this.analytics = trackRoundStart(this.analytics);
    this.persistAnalytics();
  }

  handleTouchStart(x, y) {
    if (this.state === "menu") {
      if (this.isInButton(x, y, this.startButton)) {
        this.startRound();
        return;
      }
      if (this.isInButton(x, y, this.menuSwitchRankButton)) {
        this.toggleRankView();
        return;
      }
      if (this.isInButton(x, y, this.menuRefreshRankButton)) {
        this.refreshBoard(this.rankView, true);
      }
      return;
    }

    if (this.state === "playing") {
      this.targetX = x;
      return;
    }

    if (
      this.state !== "gameover" ||
      this.awaitingReviveReward ||
      this.awaitingDoubleReward
    ) {
      return;
    }

    if (!this.roundFinalized) {
      if (!this.revived && this.isInButton(x, y, this.reviveButton)) {
        this.tryReviveReward();
        return;
      }
      if (this.isInButton(x, y, this.settleButton)) {
        this.finalizeRound();
        return;
      }
      if (this.isInButton(x, y, this.overSwitchRankButton)) {
        this.toggleRankView();
        return;
      }
      if (this.isInButton(x, y, this.overRefreshRankButton)) {
        this.refreshBoard(this.rankView, true);
      }
      return;
    }

    if (!this.doubleRewardClaimed && this.isInButton(x, y, this.doubleRewardButton)) {
      this.claimDoubleReward();
      return;
    }

    if (this.isInButton(x, y, this.retryButton)) {
      this.startRound();
      return;
    }

    if (this.isInButton(x, y, this.overSwitchRankButton)) {
      this.toggleRankView();
      return;
    }

    if (this.isInButton(x, y, this.overRefreshRankButton)) {
      this.refreshBoard(this.rankView, true);
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

  recordRewardGrant(mode) {
    this.analytics = trackRewardGrant(this.analytics, mode);
    this.persistAnalytics();
  }

  showBannerTracked() {
    if (!this.monetization.isAdEnabled()) {
      return;
    }
    this.recordAdRequest("banner");
    this.monetization.showBanner().then((result) => {
      if (result.shown && result.mode === "ad") {
        this.recordAdComplete("banner");
      }
    });
  }

  showInterstitialTracked() {
    if (!this.monetization.isAdEnabled()) {
      return;
    }
    this.recordAdRequest("interstitial");
    this.monetization.showInterstitial().then((result) => {
      if (result.shown && result.mode === "ad") {
        this.recordAdComplete("interstitial");
      }
    });
  }

  runRewardedFlow() {
    if (this.monetization.isAdEnabled()) {
      this.recordAdRequest("rewarded");
    }
    return this.monetization.runRewardedFlow().then((result) => {
      if (result.completed) {
        this.recordRewardGrant(result.mode);
      }
      if (result.mode === "ad" && result.completed) {
        this.recordAdComplete("rewarded");
      }
      return result;
    });
  }

  getRankBoard(type) {
    return this.rankBoards[type];
  }

  getRankViewName() {
    return this.rankView === "friend" ? "好友榜" : "世界榜";
  }

  toggleRankView() {
    this.rankView = this.rankView === "friend" ? "world" : "friend";
    const board = this.getRankBoard(this.rankView);
    if (board.entries.length === 0) {
      this.refreshBoard(this.rankView, false);
    }
    this.toast(`切换到${this.getRankViewName()}`);
  }

  refreshBoard(type, showToast) {
    const board = this.getRankBoard(type);
    if (!board || board.loading) {
      return;
    }
    board.loading = true;

    const manager =
      type === "friend" ? this.friendBoardManager : this.worldBoardManager;
    manager.refresh(this.profile.bestScore).then((res) => {
      board.loading = false;
      board.entries = res.entries;
      board.source = res.source;

      if (showToast) {
        const name = type === "friend" ? "好友榜" : "世界榜";
        const sourceLabel = res.source === "api" || res.source === "friend-cloud" ? "" : "(本地兜底)";
        this.toast(`${name}已刷新${sourceLabel}`);
      }
    });
  }

  tryReviveReward() {
    this.awaitingReviveReward = true;
    this.runRewardedFlow().then((result) => {
      this.awaitingReviveReward = false;
      if (!result.completed) {
        this.toast("复活失败，继续加油");
        return;
      }

      this.revived = true;
      this.state = "playing";
      this.invincibleUntil = Date.now() + GAME_CONFIG.progression.invincibleAfterReviveMs;
      this.items = this.items.filter((item) => item.type !== "bomb");
      this.message = result.mode === "ad" ? "复活成功：短暂无敌" : "公测福利复活成功";
      this.messageUntil = Date.now() + 1800;
      this.monetization.hideBanner();
    });
  }

  claimDoubleReward() {
    this.awaitingDoubleReward = true;
    this.runRewardedFlow().then((result) => {
      this.awaitingDoubleReward = false;
      if (!result.completed) {
        this.toast("奖励领取失败");
        return;
      }

      const extra = Math.floor(
        this.roundCoinGain * (GAME_CONFIG.economy.doubleRewardMultiplier - 1)
      );
      this.profile = addCoins(this.profile, extra);
      saveProfile(this.profile);

      this.roundCoinGain += extra;
      this.doubleRewardClaimed = true;
      this.roundMessage = `双倍奖励到账：额外 +${extra} 金币`;
      this.analytics.totalCoinGain += extra;
      this.persistAnalytics();
      this.toast(result.mode === "ad" ? "双倍奖励已到账" : "公测福利双倍已到账");
    });
  }

  toast(title) {
    if (!wx.showToast) {
      return;
    }
    wx.showToast({
      title,
      icon: "none",
      duration: 1100,
    });
  }

  onRoundFailed() {
    this.state = "gameover";
    this.showBannerTracked();

    if (this.revived) {
      this.finalizeRound();
      return;
    }

    this.roundMessage = this.monetization.isAdEnabled()
      ? "可看广告复活 1 次，或直接结算"
      : "公测福利：可免费复活 1 次，或直接结算";
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

    if (this.missionCompleted && this.missionBonusCoins > 0) {
      this.profile = addCoins(this.profile, this.missionBonusCoins);
      this.roundCoinGain += this.missionBonusCoins;
      this.roundMessage = `任务完成奖励 +${this.missionBonusCoins} 金币`;
    }

    saveProfile(this.profile);

    this.friendBoardManager.submitBestScore(this.profile.bestScore);
    this.worldBoardManager.submitScore(this.profile.bestScore);
    this.refreshBoard(this.rankView, false);

    this.analytics = trackRoundSettled(
      this.analytics,
      this.score,
      this.roundCoinGain
    );
    this.persistAnalytics();

    if (this.dailyBonusGain > 0) {
      this.roundMessage = `${this.roundMessage} 今日目标额外 +${this.dailyBonusGain}`.trim();
    } else if (!this.roundMessage) {
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

    this.updateBackground(dtMs);
    this.update(dtMs);
    this.render();

    requestAnimationFrame(this.loop);
  }

  updateBackground(dtMs) {
    const dtSec = dtMs / 1000;
    for (const p of this.backgroundParticles) {
      p.y += p.speed * dtSec;
      if (p.y > this.height + 4) {
        p.y = -4;
        p.x = Math.random() * this.width;
      }
    }
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

    if (this.roundMission.type === "survival" && !this.missionCompleted) {
      this.missionProgress = Math.floor(this.elapsedMs / 1000);
      if (this.missionProgress >= this.roundMission.target) {
        this.completeMission();
      }
    }

    if (now > this.comboExpireAt) {
      this.comboCount = 0;
    }

    const baseSlow =
      now < this.effectState.slowUntil ? GAME_CONFIG.props.slow.speedScale : 1;
    const feverSlow = now < this.feverUntil ? 0.9 : 1;
    const slowScale = baseSlow * feverSlow;
    const dtSec = dtMs / 1000;

    for (let i = this.items.length - 1; i >= 0; i -= 1) {
      const item = this.items[i];

      if (
        (item.type === "coin" || item.type === "gem") &&
        now < this.effectState.magnetUntil
      ) {
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
        this.onCoinCollect(1);
        continue;
      }
      if (item.type === "gem") {
        this.onCoinCollect(GAME_CONFIG.item.gemScore, true);
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
        this.message = "护盾挡住炸弹";
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
    const baseSpeed =
      GAME_CONFIG.item.baseSpeed +
      (this.level - 1) * GAME_CONFIG.item.levelSpeedGain +
      Math.random() * GAME_CONFIG.item.speedRandom;

    if (Math.random() < GAME_CONFIG.item.propRate) {
      const r = GAME_CONFIG.item.propRadius;
      this.items.push({
        type: "prop",
        propType: this.pickPropType(),
        x: r + Math.random() * (this.width - r * 2),
        y: -r,
        radius: r,
        speed: baseSpeed * 0.8,
      });
      return;
    }

    if (Math.random() < GAME_CONFIG.item.gemRate) {
      const r = GAME_CONFIG.item.gemRadius;
      this.items.push({
        type: "gem",
        x: r + Math.random() * (this.width - r * 2),
        y: -r,
        radius: r,
        speed: baseSpeed * 0.95,
      });
      return;
    }

    const bombRate = Math.min(
      GAME_CONFIG.item.maxBombRate,
      GAME_CONFIG.item.baseBombRate +
        (this.level - 1) * GAME_CONFIG.item.bombRateGainPerLevel
    );

    const isBomb = Math.random() < bombRate;
    const r = GAME_CONFIG.item.radius;
    this.items.push({
      type: isBomb ? "bomb" : "coin",
      x: r + Math.random() * (this.width - r * 2),
      y: -r,
      radius: r,
      speed: baseSpeed,
    });
  }

  pickPropType() {
    const candidates = ["shield", "magnet", "slow"];
    return candidates[Math.floor(Math.random() * candidates.length)];
  }

  onCoinCollect(basePoint, isGem) {
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
    let gain = basePoint * comboMultiplier;
    if (now < this.feverUntil) {
      gain *= GAME_CONFIG.progression.feverScoreScale;
    }
    this.score += gain;

    if (this.roundMission.type === "coin" && !this.missionCompleted) {
      this.missionProgress += 1;
      if (this.missionProgress >= this.roundMission.target) {
        this.completeMission();
      }
    }

    if (this.comboCount >= GAME_CONFIG.progression.feverTriggerCombo && now >= this.feverUntil) {
      this.feverUntil = now + GAME_CONFIG.progression.feverDurationMs;
      this.message = "狂热模式开启";
      this.messageUntil = now + 900;
      return;
    }

    if (isGem) {
      this.message = `钻石 +${gain}`;
      this.messageUntil = now + 700;
      return;
    }

    if (comboMultiplier >= 2) {
      this.message = `连击 x${comboMultiplier}`;
      this.messageUntil = now + 700;
    }
  }

  onPropCollect(propType) {
    const now = Date.now();
    if (this.roundMission.type === "prop" && !this.missionCompleted) {
      this.missionProgress += 1;
      if (this.missionProgress >= this.roundMission.target) {
        this.completeMission();
      }
    }

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

  completeMission() {
    if (this.missionCompleted) {
      return;
    }
    this.missionCompleted = true;
    this.message = `任务完成 +${this.missionBonusCoins} 金币`;
    this.messageUntil = Date.now() + 1200;
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
    this.drawBackground();

    if (this.state === "playing") {
      this.drawHud();
      this.drawItems();
      this.drawPlayer();
      this.drawMessage();
      return;
    }

    this.drawHud();
    this.drawItems();
    this.drawPlayer();
    this.drawMessage();

    if (this.state === "menu") {
      this.drawMenu();
      return;
    }

    this.drawGameOver();
  }

  drawBackground() {
    const { ctx } = this;
    const gradient = ctx.createLinearGradient(0, 0, 0, this.height);
    gradient.addColorStop(0, "#0b1228");
    gradient.addColorStop(1, GAME_CONFIG.ui.background);

    ctx.clearRect(0, 0, this.width, this.height);
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, this.width, this.height);

    for (const p of this.backgroundParticles) {
      ctx.beginPath();
      ctx.fillStyle = `rgba(191, 219, 254, ${p.alpha})`;
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  drawHud() {
    const { ctx } = this;
    const palette = GAME_CONFIG.ui;
    const now = Date.now();

    this.drawRoundedRect(12, 12, this.width - 24, 102, 12, palette.panel);
    ctx.fillStyle = palette.textPrimary;
    ctx.font = "bold 20px sans-serif";
    ctx.fillText(`分数 ${this.score}`, 24, 40);
    ctx.fillText(`Lv.${this.level}`, this.width - 112, 40);

    ctx.fillStyle = palette.textMuted;
    ctx.font = "14px sans-serif";
    const comboMultiplier = this.clamp(
      1 + Math.floor((Math.max(this.comboCount, 1) - 1) / 4),
      1,
      GAME_CONFIG.progression.maxComboMultiplier
    );
    const feverRemain = now < this.feverUntil ? Math.ceil((this.feverUntil - now) / 1000) : 0;
    ctx.fillText(
      feverRemain > 0
        ? `连击倍率 x${comboMultiplier} | 狂热 ${feverRemain}s`
        : `连击倍率 x${comboMultiplier}`,
      24,
      63
    );

    const missionProgress = Math.min(this.missionProgress, this.roundMission.target);
    const missionState = this.missionCompleted
      ? `${this.roundMission.label} (完成)`
      : `${this.roundMission.label} (${missionProgress}/${this.roundMission.target})`;
    ctx.fillText(missionState, 24, 84);

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
      104
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

      if (item.type === "gem") {
        ctx.beginPath();
        ctx.fillStyle = "#38bdf8";
        ctx.arc(item.x, item.y, item.radius, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "#f8fafc";
        ctx.font = "bold 12px sans-serif";
        ctx.fillText("◆", item.x - 5, item.y + 4);
        continue;
      }

      ctx.beginPath();
      ctx.fillStyle = item.type === "coin" ? GAME_CONFIG.ui.accent : GAME_CONFIG.ui.danger;
      ctx.arc(item.x, item.y, item.radius, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = item.type === "coin" ? "#92400e" : "#fee2e2";
      ctx.font = "bold 14px sans-serif";
      ctx.fillText(item.type === "coin" ? "$" : "!", item.x - 4, item.y + 5);
    }
  }

  drawPlayer() {
    const { ctx } = this;
    const x = this.player.x - this.player.w / 2;
    const y = this.player.y - this.player.h / 2;

    this.drawRoundedRect(x, y, this.player.w, this.player.h, 10, "#38bdf8");

    if (Date.now() <= this.invincibleUntil) {
      ctx.strokeStyle = "#22d3ee";
      ctx.lineWidth = 3;
      this.strokeRoundedRect(x - 3, y - 3, this.player.w + 6, this.player.h + 6, 12);
    }

    ctx.fillStyle = GAME_CONFIG.ui.textPrimary;
    ctx.font = "12px sans-serif";
    ctx.fillText("接金币盘", x + 9, y + 14);
  }

  drawMessage() {
    if (!this.message || Date.now() > this.messageUntil) {
      return;
    }
    const { ctx } = this;
    ctx.fillStyle = "#e2e8f0";
    ctx.font = "bold 18px sans-serif";
    const w = ctx.measureText(this.message).width;
    ctx.fillText(this.message, (this.width - w) / 2, this.height * 0.25);
  }

  drawMenu() {
    const { ctx } = this;
    const panelX = 14;
    const panelY = this.height * 0.05;
    const panelW = this.width - 28;
    const panelH = this.height * 0.9;

    this.drawRoundedRect(panelX, panelY, panelW, panelH, 18, GAME_CONFIG.ui.card);

    ctx.fillStyle = GAME_CONFIG.ui.textPrimary;
    ctx.font = "bold 30px sans-serif";
    const title = GAME_CONFIG.ui.title;
    ctx.fillText(title, (this.width - ctx.measureText(title).width) / 2, panelY + 44);

    ctx.fillStyle = "#93c5fd";
    ctx.font = "14px sans-serif";
    const modeLabel = this.monetization.isAdEnabled()
      ? "广告变现模式"
      : GAME_CONFIG.monetization.prelaunchTag;
    ctx.fillText(modeLabel, (this.width - ctx.measureText(modeLabel).width) / 2, panelY + 66);

    ctx.fillStyle = GAME_CONFIG.ui.textMuted;
    ctx.font = "15px sans-serif";
    ctx.fillText(`历史最高: ${this.profile.bestScore}`, panelX + 18, panelY + 94);
    ctx.fillText(`总金币: ${this.profile.totalCoins}`, panelX + 18, panelY + 116);
    ctx.fillText(`连续打卡: ${this.profile.streakDays} 天`, panelX + 18, panelY + 138);
    ctx.fillText(
      `今日目标: ${this.profile.dailyProgress}/${this.profile.dailyTarget}`,
      panelX + 18,
      panelY + 160
    );

    this.drawLeaderboardCard(panelX + 14, panelY + 172, panelW - 28, 152, 5);
    this.drawAnalyticsCard(panelX + 14, panelY + 332, panelW - 28, 118);

    this.drawButton(this.startButton, "开始闯关", "#f59e0b");
    this.drawButton(
      this.menuSwitchRankButton,
      this.rankView === "friend" ? "切换到世界榜" : "切换到好友榜",
      "#1d4ed8"
    );
    this.drawButton(
      this.menuRefreshRankButton,
      `刷新${this.getRankViewName()}`,
      "#22c55e"
    );
  }

  drawGameOver() {
    const { ctx } = this;
    const panelX = 14;
    const panelY = this.height * 0.04;
    const panelW = this.width - 28;
    const panelH = this.height * 0.92;

    this.drawRoundedRect(panelX, panelY, panelW, panelH, 18, "rgba(2, 6, 23, 0.85)");

    ctx.fillStyle = GAME_CONFIG.ui.textPrimary;
    ctx.font = "bold 34px sans-serif";
    const over = "本局结束";
    ctx.fillText(over, (this.width - ctx.measureText(over).width) / 2, panelY + 48);

    ctx.font = "19px sans-serif";
    const scoreText = `得分 ${this.score} | 结算金币 ${this.roundCoinGain}`;
    ctx.fillText(scoreText, (this.width - ctx.measureText(scoreText).width) / 2, panelY + 78);

    ctx.fillStyle = GAME_CONFIG.ui.textMuted;
    ctx.font = "15px sans-serif";
    const profileText = `最高 ${this.profile.bestScore} | 总金币 ${this.profile.totalCoins}`;
    ctx.fillText(
      profileText,
      (this.width - ctx.measureText(profileText).width) / 2,
      panelY + 104
    );

    if (this.roundMessage) {
      ctx.fillStyle = "#86efac";
      ctx.font = "14px sans-serif";
      ctx.fillText(
        this.roundMessage,
        (this.width - ctx.measureText(this.roundMessage).width) / 2,
        panelY + 126
      );
    }

    this.drawLeaderboardCard(panelX + 14, panelY + 138, panelW - 28, 152, 5);

    if (!this.roundFinalized) {
      this.drawButton(this.settleButton, "直接结算", "#f59e0b");
      if (!this.revived) {
        this.drawButton(
          this.reviveButton,
          this.awaitingReviveReward
            ? "处理中..."
            : this.monetization.getRewardLabel("看广告复活"),
          "#22c55e"
        );
      }
    } else {
      this.drawButton(
        this.doubleRewardButton,
        this.awaitingDoubleReward
          ? "处理中..."
          : this.doubleRewardClaimed
          ? "双倍奖励已领取"
          : this.monetization.getRewardLabel("看广告双倍奖励"),
        this.doubleRewardClaimed ? "#475569" : "#22c55e"
      );
      this.drawButton(this.retryButton, "再来一局", "#f59e0b");
    }

    this.drawButton(
      this.overSwitchRankButton,
      this.rankView === "friend" ? "切到世界榜" : "切到好友榜",
      "#1d4ed8"
    );
    this.drawButton(
      this.overRefreshRankButton,
      `刷新${this.getRankViewName()}`,
      "#22c55e"
    );
  }

  drawLeaderboardCard(x, y, w, h, rows) {
    const { ctx } = this;
    this.drawRoundedRect(x, y, w, h, 12, GAME_CONFIG.ui.cardSoft);

    const board = this.getRankBoard(this.rankView);
    const sourceLabel = this.getBoardSourceLabel(this.rankView, board.source);

    ctx.fillStyle = "#f8fafc";
    ctx.font = "bold 16px sans-serif";
    ctx.fillText(this.getRankViewName(), x + 12, y + 24);

    ctx.fillStyle = "#94a3b8";
    ctx.font = "12px sans-serif";
    ctx.fillText(sourceLabel, x + 90, y + 24);
    if (board.loading) {
      ctx.fillText("刷新中...", x + w - 66, y + 24);
    }

    const entries = board.entries.slice(0, rows);
    ctx.font = "14px sans-serif";
    if (entries.length === 0) {
      ctx.fillStyle = "#cbd5e1";
      ctx.fillText("暂无数据，点击刷新", x + 12, y + 58);
      return;
    }

    entries.forEach((entry, index) => {
      const rowY = y + 50 + index * 19;
      ctx.fillStyle = entry.isSelf ? "#fde68a" : "#e2e8f0";
      const name = entry.nickname.length > 8 ? `${entry.nickname.slice(0, 8)}…` : entry.nickname;
      ctx.fillText(`${entry.rank}. ${name}`, x + 12, rowY);
      const scoreText = `${entry.score}`;
      ctx.fillText(scoreText, x + w - ctx.measureText(scoreText).width - 12, rowY);
    });
  }

  getBoardSourceLabel(type, source) {
    if (type === "friend") {
      if (source === "friend-cloud") {
        return "微信好友数据";
      }
      if (source === "cache") {
        return "本地缓存";
      }
      return "本地兜底";
    }
    if (source === "api") {
      return "世界榜API";
    }
    if (source === "cache") {
      return "本地缓存";
    }
    return "世界榜兜底";
  }

  drawAnalyticsCard(x, y, w, h) {
    const { ctx } = this;
    const stats = this.analyticsSnapshot;
    this.drawRoundedRect(x, y, w, h, 12, "rgba(15, 23, 42, 0.95)");

    ctx.fillStyle = "#f8fafc";
    ctx.font = "bold 16px sans-serif";
    ctx.fillText("UV与留存看板", x + 12, y + 24);

    ctx.fillStyle = "#cbd5e1";
    ctx.font = "13px sans-serif";
    ctx.fillText(
      `会话(UV参考): ${stats.sessions} | 局完成率: ${stats.completionRate}%`,
      x + 12,
      y + 50
    );
    ctx.fillText(
      `D1留存: ${stats.d1Retained ? "已达成" : "未达成"} | 激励完成: ${stats.rewardCompletionRate}%`,
      x + 12,
      y + 72
    );
    ctx.fillText(
      `ARPU估算: ${stats.arpu} 元 | 免费奖励: ${stats.freeRewardGrant}`,
      x + 12,
      y + 94
    );
  }

  drawButton(rect, text, color) {
    const { ctx } = this;
    this.drawRoundedRect(rect.x, rect.y, rect.w, rect.h, 12, color || "#f59e0b");
    ctx.fillStyle = GAME_CONFIG.ui.buttonText;
    ctx.font = "bold 18px sans-serif";
    const textW = ctx.measureText(text).width;
    ctx.fillText(text, rect.x + (rect.w - textW) / 2, rect.y + 30);
  }

  drawRoundedRect(x, y, w, h, radius, color) {
    const { ctx } = this;
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.lineTo(x + w - radius, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
    ctx.lineTo(x + w, y + h - radius);
    ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
    ctx.lineTo(x + radius, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
    ctx.lineTo(x, y + radius);
    ctx.quadraticCurveTo(x, y, x + radius, y);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  }

  strokeRoundedRect(x, y, w, h, radius) {
    const { ctx } = this;
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.lineTo(x + w - radius, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
    ctx.lineTo(x + w, y + h - radius);
    ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
    ctx.lineTo(x + radius, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
    ctx.lineTo(x, y + radius);
    ctx.quadraticCurveTo(x, y, x + radius, y);
    ctx.closePath();
    ctx.stroke();
  }
}

module.exports = {
  MiniGameApp,
};
