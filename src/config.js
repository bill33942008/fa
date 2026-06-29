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
    baseSpeed: 130,
    levelSpeedGain: 18,
    speedRandom: 70,
    spawnInterval: 920,
    minSpawnInterval: 280,
    spawnIntervalGainPerLevel: 58,
    baseBombRate: 0.08,
    maxBombRate: 0.47,
    bombRateGainPerLevel: 0.032,
  },
  progression: {
    levelDurationMs: 18000,
    comboWindowMs: 1250,
    maxComboMultiplier: 5,
    invincibleAfterReviveMs: 2200,
  },
  retention: {
    dailyTargetMin: 30,
    dailyTargetMax: 120,
    interstitialGapRounds: 3,
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
