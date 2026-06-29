const { GAME_CONFIG } = require("./config");
const { AdManager } = require("./ad-manager");
const {
  loadProfile,
  saveProfile,
  refreshDaily,
  updateProfileAfterRound,
} = require("./storage");

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

    this.state = "menu";
    this.lastTimestamp = 0;
    this.awaitingReward = false;
    this.roundMessage = "";

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
    this.adManager.showBanner();
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
      y: this.height * 0.64,
      w: 224,
      h: 56,
    };
    this.retryButton = {
      x: this.width / 2 - 112,
      y: this.height * 0.62,
      w: 224,
      h: 56,
    };
    this.reviveButton = {
      x: this.width / 2 - 112,
      y: this.height * 0.72,
      w: 224,
      h: 56,
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
    this.message = "按住并移动手指，接金币躲炸弹";
    this.messageUntil = Date.now() + 2600;
  }

  startRound() {
    this.resetRound();
    this.state = "playing";
    this.adManager.hideBanner();
  }

  handleTouchStart(x, y) {
    if (this.state === "menu") {
      this.startRound();
      return;
    }

    if (this.state === "playing") {
      this.targetX = x;
      return;
    }

    if (this.state !== "gameover" || this.awaitingReward) {
      return;
    }

    if (!this.revived && this.isInButton(x, y, this.reviveButton)) {
      this.tryReviveByAd();
      return;
    }

    if (this.isInButton(x, y, this.retryButton)) {
      this.startRound();
    }
  }

  isInButton(x, y, btn) {
    return x >= btn.x && x <= btn.x + btn.w && y >= btn.y && y <= btn.y + btn.h;
  }

  tryReviveByAd() {
    this.awaitingReward = true;
    this.adManager.showReviveRewarded().then((completed) => {
      this.awaitingReward = false;
      if (!completed) {
        this.toast("广告未完整播放，复活失败");
        return;
      }
      this.revived = true;
      this.state = "playing";
      this.invincibleUntil = Date.now() + GAME_CONFIG.progression.invincibleAfterReviveMs;
      this.items = this.items.filter((item) => item.type === "coin");
      this.message = "复活成功：短暂无敌";
      this.messageUntil = Date.now() + 1800;
      this.adManager.hideBanner();
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

  endRound() {
    this.state = "gameover";
    this.adManager.showBanner();

    const beforeComplete = this.profile.dailyCompleted;
    this.profile = updateProfileAfterRound(
      this.profile,
      this.score,
      GAME_CONFIG.retention
    );
    saveProfile(this.profile);

    if (!beforeComplete && this.profile.dailyCompleted) {
      this.roundMessage = "今日目标达成，额外+50金币";
    } else {
      this.roundMessage = "";
    }

    if (
      this.profile.totalRounds > 0 &&
      this.profile.totalRounds % GAME_CONFIG.retention.interstitialGapRounds === 0
    ) {
      this.adManager.showInterstitial();
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

    const now = Date.now();
    if (now > this.comboExpireAt) {
      this.comboCount = 0;
    }

    const dtSec = dtMs / 1000;
    for (let i = this.items.length - 1; i >= 0; i -= 1) {
      const item = this.items[i];
      item.y += item.speed * dtSec;

      if (item.y - item.radius > this.height) {
        this.items.splice(i, 1);
        continue;
      }

      if (this.hitPlayer(item)) {
        this.items.splice(i, 1);
        if (item.type === "coin") {
          this.onCoinCollect();
          continue;
        }

        if (Date.now() <= this.invincibleUntil) {
          continue;
        }

        this.endRound();
        return;
      }
    }
  }

  spawnItem() {
    const bombRate = Math.min(
      GAME_CONFIG.item.maxBombRate,
      GAME_CONFIG.item.baseBombRate +
        (this.level - 1) * GAME_CONFIG.item.bombRateGainPerLevel
    );

    const isBomb = Math.random() < bombRate;
    const radius = GAME_CONFIG.item.radius;

    this.items.push({
      type: isBomb ? "bomb" : "coin",
      x: radius + Math.random() * (this.width - radius * 2),
      y: -radius,
      radius,
      speed:
        GAME_CONFIG.item.baseSpeed +
        (this.level - 1) * GAME_CONFIG.item.levelSpeedGain +
        Math.random() * GAME_CONFIG.item.speedRandom,
    });
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

    ctx.fillStyle = palette.panel;
    ctx.fillRect(12, 12, this.width - 24, 72);

    ctx.fillStyle = palette.textPrimary;
    ctx.font = "bold 20px sans-serif";
    ctx.fillText(`分数 ${this.score}`, 24, 42);
    ctx.fillText(`Lv.${this.level}`, this.width - 110, 42);

    ctx.fillStyle = palette.textMuted;
    ctx.font = "16px sans-serif";
    const comboMultiplier = this.clamp(
      1 + Math.floor((Math.max(this.comboCount, 1) - 1) / 4),
      1,
      GAME_CONFIG.progression.maxComboMultiplier
    );
    ctx.fillText(`连击倍率 x${comboMultiplier}`, 24, 68);
  }

  drawItems() {
    const { ctx } = this;
    for (const item of this.items) {
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
    const palette = GAME_CONFIG.ui;

    const x = this.player.x - this.player.w / 2;
    const y = this.player.y - this.player.h / 2;

    ctx.fillStyle = "#38bdf8";
    ctx.fillRect(x, y, this.player.w, this.player.h);

    if (Date.now() <= this.invincibleUntil) {
      ctx.strokeStyle = "#22d3ee";
      ctx.lineWidth = 3;
      ctx.strokeRect(x - 3, y - 3, this.player.w + 6, this.player.h + 6);
    }

    ctx.fillStyle = palette.textPrimary;
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
    ctx.fillText(this.message, (this.width - w) / 2, this.height * 0.22);
  }

  drawMenu() {
    const { ctx } = this;
    const palette = GAME_CONFIG.ui;
    const panelX = 22;
    const panelY = this.height * 0.2;
    const panelW = this.width - 44;
    const panelH = this.height * 0.54;

    ctx.fillStyle = "rgba(15, 23, 42, 0.82)";
    ctx.fillRect(panelX, panelY, panelW, panelH);

    ctx.fillStyle = palette.textPrimary;
    ctx.font = "bold 30px sans-serif";
    const title = GAME_CONFIG.ui.title;
    const titleW = ctx.measureText(title).width;
    ctx.fillText(title, (this.width - titleW) / 2, panelY + 56);

    ctx.font = "18px sans-serif";
    ctx.fillText("上手简单：按住并左右移动", panelX + 22, panelY + 104);
    ctx.fillText("玩得越久：掉落越快、炸弹越多", panelX + 22, panelY + 136);

    ctx.fillStyle = palette.textMuted;
    ctx.font = "16px sans-serif";
    ctx.fillText(`历史最高：${this.profile.bestScore}`, panelX + 22, panelY + 182);
    ctx.fillText(`连续打卡：${this.profile.streakDays} 天`, panelX + 22, panelY + 212);
    ctx.fillText(
      `今日目标：${this.profile.dailyProgress}/${this.profile.dailyTarget}`,
      panelX + 22,
      panelY + 242
    );

    this.drawButton(this.startButton, "开始闯关");
  }

  drawGameOver() {
    const { ctx } = this;
    const palette = GAME_CONFIG.ui;

    ctx.fillStyle = "rgba(2, 6, 23, 0.75)";
    ctx.fillRect(0, 0, this.width, this.height);

    ctx.fillStyle = palette.textPrimary;
    ctx.font = "bold 34px sans-serif";
    const over = "本局结束";
    ctx.fillText(over, (this.width - ctx.measureText(over).width) / 2, this.height * 0.33);

    ctx.font = "22px sans-serif";
    const scoreText = `得分 ${this.score}`;
    ctx.fillText(
      scoreText,
      (this.width - ctx.measureText(scoreText).width) / 2,
      this.height * 0.4
    );

    ctx.font = "16px sans-serif";
    ctx.fillStyle = palette.textMuted;
    const profileText = `最高 ${this.profile.bestScore} | 总金币 ${this.profile.totalCoins}`;
    ctx.fillText(
      profileText,
      (this.width - ctx.measureText(profileText).width) / 2,
      this.height * 0.45
    );

    if (this.roundMessage) {
      ctx.fillStyle = "#86efac";
      const rewardText = this.roundMessage;
      ctx.fillText(
        rewardText,
        (this.width - ctx.measureText(rewardText).width) / 2,
        this.height * 0.5
      );
    }

    this.drawButton(this.retryButton, "再来一局");
    if (!this.revived) {
      this.drawButton(this.reviveButton, this.awaitingReward ? "广告加载中..." : "看广告复活");
    }
  }

  drawButton(rect, text) {
    const { ctx } = this;
    ctx.fillStyle = "#f59e0b";
    ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
    ctx.fillStyle = "#111827";
    ctx.font = "bold 22px sans-serif";
    const w = ctx.measureText(text).width;
    ctx.fillText(text, rect.x + (rect.w - w) / 2, rect.y + 36);
  }
}

module.exports = {
  MiniGameApp,
};
