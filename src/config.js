const GAME_CONFIG = {
  ui: {
    title: "金币冲冲冲",
    background: "#0f172a",
    panel: "#1e293b",
    accent: "#facc15",
    danger: "#ef4444",
    textPrimary: "#f8fafc",
    textMuted: "#94a3b8",
  },
  player: {
    width: 76,
    height: 20,
    speedFollow: 0.23,
  },
  item: {
    radius: 14,
    propRadius: 15,
    baseSpeed: 130,
    levelSpeedGain: 18,
    speedRandom: 70,
    spawnInterval: 920,
    minSpawnInterval: 280,
    spawnIntervalGainPerLevel: 58,
    baseBombRate: 0.08,
    maxBombRate: 0.47,
    bombRateGainPerLevel: 0.032,
    propRate: 0.12,
  },
  props: {
    shield: {
      icon: "盾",
      color: "#34d399",
      bombBlockCount: 1,
    },
    magnet: {
      icon: "磁",
      color: "#60a5fa",
      durationMs: 7000,
      attractRange: 170,
      attractSpeed: 360,
    },
    slow: {
      icon: "缓",
      color: "#c084fc",
      durationMs: 5200,
      speedScale: 0.62,
    },
  },
  progression: {
    levelDurationMs: 18000,
    comboWindowMs: 1250,
    maxComboMultiplier: 5,
    invincibleAfterReviveMs: 2200,
  },
  economy: {
    dailyBonusCoins: 50,
    doubleRewardMultiplier: 2,
  },
  retention: {
    dailyTargetMin: 30,
    dailyTargetMax: 120,
    interstitialGapRounds: 3,
  },
  analytics: {
    adRevenuePerImpression: {
      banner: 0.0025,
      interstitial: 0.02,
      rewarded: 0.08,
    },
  },
  leaderboard: {
    keyName: "coinRushBestScore",
    maxEntries: 20,
    fallbackNames: [
      "小虎",
      "阿飞",
      "可乐",
      "米粒",
      "柚子",
      "晨光",
      "木木",
      "星辰",
      "豆包",
      "南风",
    ],
  },
};

const AD_UNIT_IDS = {
  banner: "adunit-xxxxxxxxxxxxxxxx",
  rewardedVideo: "adunit-yyyyyyyyyyyyyyyy",
  interstitial: "adunit-zzzzzzzzzzzzzzzz",
};

module.exports = {
  GAME_CONFIG,
  AD_UNIT_IDS,
};
