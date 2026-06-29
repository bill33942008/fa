const ANALYTICS_KEY = "coinRushAnalyticsV1";

function getTodayKey(date = new Date()) {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function addDays(dayKey, offset) {
  const date = new Date(`${dayKey}T00:00:00`);
  date.setDate(date.getDate() + offset);
  return getTodayKey(date);
}

function getDefaultAnalytics() {
  return {
    firstSeenDay: "",
    lastSeenDay: "",
    activeDayMap: {},
    sessions: 0,
    roundsStarted: 0,
    roundsSettled: 0,
    totalScore: 0,
    totalCoinGain: 0,
    ad: {
      banner: { requested: 0, completed: 0 },
      interstitial: { requested: 0, completed: 0 },
      rewarded: { requested: 0, completed: 0 },
    },
    adRevenue: 0,
  };
}

function loadAnalytics() {
  try {
    const data = wx.getStorageSync(ANALYTICS_KEY);
    if (!data) {
      return getDefaultAnalytics();
    }
    const merged = { ...getDefaultAnalytics(), ...data };
    merged.ad = { ...getDefaultAnalytics().ad, ...(data.ad || {}) };
    merged.ad.banner = {
      ...getDefaultAnalytics().ad.banner,
      ...(merged.ad.banner || {}),
    };
    merged.ad.interstitial = {
      ...getDefaultAnalytics().ad.interstitial,
      ...(merged.ad.interstitial || {}),
    };
    merged.ad.rewarded = {
      ...getDefaultAnalytics().ad.rewarded,
      ...(merged.ad.rewarded || {}),
    };
    return merged;
  } catch (error) {
    return getDefaultAnalytics();
  }
}

function saveAnalytics(analytics) {
  wx.setStorageSync(ANALYTICS_KEY, analytics);
}

function markSessionStart(analytics) {
  const today = getTodayKey();
  analytics.sessions += 1;
  if (!analytics.firstSeenDay) {
    analytics.firstSeenDay = today;
  }
  analytics.lastSeenDay = today;
  analytics.activeDayMap[today] = 1;
  return analytics;
}

function trackRoundStart(analytics) {
  analytics.roundsStarted += 1;
  return analytics;
}

function trackRoundSettled(analytics, score, coinGain) {
  analytics.roundsSettled += 1;
  analytics.totalScore += score;
  analytics.totalCoinGain += coinGain;
  return analytics;
}

function trackAdRequest(analytics, adType) {
  if (!analytics.ad[adType]) {
    return analytics;
  }
  analytics.ad[adType].requested += 1;
  return analytics;
}

function trackAdComplete(analytics, adType, revenue) {
  if (!analytics.ad[adType]) {
    return analytics;
  }
  analytics.ad[adType].completed += 1;
  analytics.adRevenue += revenue;
  return analytics;
}

function getAnalyticsSnapshot(analytics) {
  const rewardedReq = analytics.ad.rewarded.requested;
  const rewardedDone = analytics.ad.rewarded.completed;
  const rewardCompletionRate =
    rewardedReq > 0 ? Math.round((rewardedDone / rewardedReq) * 1000) / 10 : 0;

  const completionRate =
    analytics.roundsStarted > 0
      ? Math.round((analytics.roundsSettled / analytics.roundsStarted) * 1000) / 10
      : 0;

  const arpu = Math.round(analytics.adRevenue * 10000) / 10000;
  const day1Key = analytics.firstSeenDay ? addDays(analytics.firstSeenDay, 1) : "";
  const d1Retained = day1Key ? Boolean(analytics.activeDayMap[day1Key]) : false;

  return {
    activeDays: Object.keys(analytics.activeDayMap).length,
    d1Retained,
    completionRate,
    rewardCompletionRate,
    arpu,
    sessions: analytics.sessions,
    rounds: analytics.roundsSettled,
  };
}

module.exports = {
  loadAnalytics,
  saveAnalytics,
  markSessionStart,
  trackRoundStart,
  trackRoundSettled,
  trackAdRequest,
  trackAdComplete,
  getAnalyticsSnapshot,
};
