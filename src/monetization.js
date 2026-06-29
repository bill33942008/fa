class MonetizationBridge {
  constructor(adManager, config) {
    this.adManager = adManager;
    this.config = config;
  }

  init(screenWidth, screenHeight) {
    if (!this.config.adEnabled) {
      return;
    }
    this.adManager.init(screenWidth, screenHeight);
  }

  isAdEnabled() {
    return Boolean(this.config.adEnabled);
  }

  showBanner() {
    if (!this.config.adEnabled) {
      return Promise.resolve({ shown: false, mode: "disabled" });
    }
    return this.adManager.showBanner().then((shown) => ({ shown, mode: "ad" }));
  }

  hideBanner() {
    if (!this.config.adEnabled) {
      return;
    }
    this.adManager.hideBanner();
  }

  showInterstitial() {
    if (!this.config.adEnabled) {
      return Promise.resolve({ shown: false, mode: "disabled" });
    }
    return this.adManager
      .showInterstitial()
      .then((shown) => ({ shown, mode: "ad" }));
  }

  runRewardedFlow() {
    if (!this.config.adEnabled) {
      return Promise.resolve({
        completed: Boolean(this.config.allowFreeRewardInPrelaunch),
        mode: "free",
      });
    }

    return this.adManager.showRewardedVideo().then((completed) => ({
      completed,
      mode: "ad",
    }));
  }

  getRewardLabel(baseText) {
    if (this.config.adEnabled) {
      return baseText;
    }
    return `${baseText.replace("看广告", "免费领取")}(${this.config.prelaunchTag})`;
  }
}

module.exports = {
  MonetizationBridge,
};
