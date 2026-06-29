const { AD_UNIT_IDS } = require("./config");

class AdManager {
  constructor() {
    this.bannerAd = null;
    this.rewardedVideoAd = null;
    this.interstitialAd = null;
    this.hasAdApi = typeof wx !== "undefined";
  }

  init(screenWidth, screenHeight) {
    if (!this.hasAdApi) {
      return;
    }

    this._initBanner(screenWidth, screenHeight);
    this._initRewarded();
    this._initInterstitial();
  }

  _initBanner(screenWidth, screenHeight) {
    if (!wx.createBannerAd) {
      return;
    }

    this.bannerAd = wx.createBannerAd({
      adUnitId: AD_UNIT_IDS.banner,
      style: {
        left: 0,
        top: screenHeight - 96,
        width: screenWidth,
      },
    });

    this.bannerAd.onResize((res) => {
      this.bannerAd.style.top = screenHeight - res.height;
      this.bannerAd.style.left = (screenWidth - res.width) / 2;
    });

    this.bannerAd.onError(() => {});
  }

  _initRewarded() {
    if (!wx.createRewardedVideoAd) {
      return;
    }
    this.rewardedVideoAd = wx.createRewardedVideoAd({
      adUnitId: AD_UNIT_IDS.rewardedVideo,
    });
    this.rewardedVideoAd.onError(() => {});
  }

  _initInterstitial() {
    if (!wx.createInterstitialAd) {
      return;
    }
    this.interstitialAd = wx.createInterstitialAd({
      adUnitId: AD_UNIT_IDS.interstitial,
    });
    this.interstitialAd.onError(() => {});
  }

  showBanner() {
    if (this.bannerAd) {
      this.bannerAd.show().catch(() => {});
    }
  }

  hideBanner() {
    if (this.bannerAd) {
      this.bannerAd.hide();
    }
  }

  showInterstitial() {
    if (!this.interstitialAd) {
      return;
    }
    this.interstitialAd.show().catch(() => {});
  }

  showReviveRewarded() {
    if (!this.rewardedVideoAd) {
      return Promise.resolve(false);
    }

    return new Promise((resolve) => {
      const onClose = (result) => {
        cleanup();
        const completed = !result || result.isEnded;
        resolve(Boolean(completed));
      };

      const onError = () => {
        cleanup();
        resolve(false);
      };

      const cleanup = () => {
        this.rewardedVideoAd.offClose(onClose);
        this.rewardedVideoAd.offError(onError);
      };

      this.rewardedVideoAd.onClose(onClose);
      this.rewardedVideoAd.onError(onError);

      this.rewardedVideoAd
        .show()
        .catch(() =>
          this.rewardedVideoAd
            .load()
            .then(() => this.rewardedVideoAd.show())
            .catch(() => {
              cleanup();
              resolve(false);
            })
        );
    });
  }
}

module.exports = {
  AdManager,
};
