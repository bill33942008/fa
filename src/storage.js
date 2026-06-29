const PROFILE_KEY = "coinRushProfileV1";

function getTodayKey(date = new Date()) {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function getDefaultProfile() {
  return {
    bestScore: 0,
    totalCoins: 0,
    totalRounds: 0,
    streakDays: 0,
    lastPlayDate: "",
    dailyTarget: 50,
    dailyTargetDate: getTodayKey(),
    dailyProgress: 0,
    dailyCompleted: false,
  };
}

function loadProfile() {
  try {
    const data = wx.getStorageSync(PROFILE_KEY);
    if (!data) {
      return getDefaultProfile();
    }
    return { ...getDefaultProfile(), ...data };
  } catch (error) {
    return getDefaultProfile();
  }
}

function saveProfile(profile) {
  wx.setStorageSync(PROFILE_KEY, profile);
}

function getDayDiff(fromDay, toDay) {
  if (!fromDay || !toDay) {
    return 0;
  }
  const from = new Date(`${fromDay}T00:00:00`);
  const to = new Date(`${toDay}T00:00:00`);
  const ms = to.getTime() - from.getTime();
  return Math.round(ms / 86400000);
}

function getDailyTarget(todayKey, min, max) {
  const hash = todayKey
    .split("-")
    .join("")
    .split("")
    .reduce((acc, digit) => acc + Number(digit), 0);
  const span = max - min + 1;
  return min + (hash % span);
}

function refreshDaily(profile, retentionConfig) {
  const today = getTodayKey();
  const changedDay = profile.dailyTargetDate !== today;

  if (changedDay) {
    profile.dailyTargetDate = today;
    profile.dailyTarget = getDailyTarget(
      today,
      retentionConfig.dailyTargetMin,
      retentionConfig.dailyTargetMax
    );
    profile.dailyProgress = 0;
    profile.dailyCompleted = false;
  }

  return profile;
}

function updateProfileAfterRound(profile, score, retentionConfig) {
  const today = getTodayKey();
  refreshDaily(profile, retentionConfig);

  const dayDiff = getDayDiff(profile.lastPlayDate, today);
  if (!profile.lastPlayDate) {
    profile.streakDays = 1;
  } else if (dayDiff === 1) {
    profile.streakDays += 1;
  } else if (dayDiff > 1) {
    profile.streakDays = 1;
  }
  profile.lastPlayDate = today;

  profile.totalRounds += 1;
  profile.totalCoins += score;
  profile.bestScore = Math.max(profile.bestScore, score);
  profile.dailyProgress += score;

  if (!profile.dailyCompleted && profile.dailyProgress >= profile.dailyTarget) {
    profile.dailyCompleted = true;
    profile.totalCoins += 50;
  }

  return profile;
}

module.exports = {
  loadProfile,
  saveProfile,
  refreshDaily,
  updateProfileAfterRound,
};
