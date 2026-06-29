const LEADERBOARD_CACHE_KEY = "coinRushLeaderboardCacheV1";

function loadCache() {
  try {
    const data = wx.getStorageSync(LEADERBOARD_CACHE_KEY);
    return Array.isArray(data) ? data : [];
  } catch (error) {
    return [];
  }
}

function saveCache(entries) {
  wx.setStorageSync(LEADERBOARD_CACHE_KEY, entries);
}

function parseCloudScore(friendData, keyName) {
  const kvList = friendData.KVDataList || [];
  const scoreItem = kvList.find((kv) => kv.key === keyName);
  const value = scoreItem ? Number(scoreItem.value) : 0;
  return Number.isFinite(value) ? value : 0;
}

class LeaderboardManager {
  constructor(config) {
    this.config = config;
    this.entries = loadCache();
  }

  getCachedEntries() {
    return this.entries.slice(0, this.config.maxEntries);
  }

  refresh(bestScore) {
    return new Promise((resolve) => {
      if (!wx.getFriendCloudStorage) {
        const fallback = this.buildFallback(bestScore);
        this.entries = fallback;
        saveCache(fallback);
        resolve({ entries: fallback, source: "fallback" });
        return;
      }

      wx.getFriendCloudStorage({
        keyList: [this.config.keyName],
        success: (res) => {
          const data = Array.isArray(res.data) ? res.data : [];
          const parsed = data
            .map((friend) => ({
              nickname: friend.nickname || "好友玩家",
              score: parseCloudScore(friend, this.config.keyName),
              avatarUrl: friend.avatarUrl || "",
              isSelf: false,
            }))
            .filter((item) => Number.isFinite(item.score));

          const withSelf = this.mergeSelfEntry(parsed, bestScore);
          const sorted = withSelf
            .sort((a, b) => b.score - a.score)
            .slice(0, this.config.maxEntries)
            .map((entry, index) => ({ ...entry, rank: index + 1 }));

          this.entries = sorted;
          saveCache(sorted);
          resolve({ entries: sorted, source: "friend-cloud" });
        },
        fail: () => {
          const fallback = this.buildFallback(bestScore);
          this.entries = fallback;
          saveCache(fallback);
          resolve({ entries: fallback, source: "fallback" });
        },
      });
    });
  }

  submitBestScore(bestScore) {
    if (wx.setUserCloudStorage) {
      wx.setUserCloudStorage({
        KVDataList: [{ key: this.config.keyName, value: `${bestScore}` }],
        success: () => {},
        fail: () => {},
      });
    }
  }

  mergeSelfEntry(entries, bestScore) {
    const hasSelf = entries.some((entry) => entry.isSelf);
    if (hasSelf) {
      return entries;
    }
    return entries.concat([
      {
        nickname: "我",
        score: bestScore,
        avatarUrl: "",
        isSelf: true,
      },
    ]);
  }

  buildFallback(bestScore) {
    const names = this.config.fallbackNames;
    const base = Math.max(bestScore, 45);
    const generated = names.map((name, index) => {
      const seed = (index * 37 + base) % 55;
      const score = Math.max(8, base + 60 - seed - index * 4);
      return {
        nickname: name,
        score,
        avatarUrl: "",
        isSelf: false,
      };
    });

    generated.push({
      nickname: "我",
      score: bestScore,
      avatarUrl: "",
      isSelf: true,
    });

    return generated
      .sort((a, b) => b.score - a.score)
      .slice(0, this.config.maxEntries)
      .map((entry, index) => ({ ...entry, rank: index + 1 }));
  }
}

module.exports = {
  LeaderboardManager,
};
