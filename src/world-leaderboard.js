const WORLD_BOARD_CACHE_KEY = "coinRushWorldBoardCacheV1";

function loadCache() {
  try {
    const cache = wx.getStorageSync(WORLD_BOARD_CACHE_KEY);
    return Array.isArray(cache) ? cache : [];
  } catch (error) {
    return [];
  }
}

function saveCache(entries) {
  wx.setStorageSync(WORLD_BOARD_CACHE_KEY, entries);
}

function buildFallbackEntries(config, bestScore) {
  const base = Math.max(bestScore, 60);
  const entries = config.fallbackNames.map((name, index) => {
    const seed = (base + index * 29) % 90;
    return {
      nickname: name,
      score: Math.max(10, base + 150 - seed - index * 6),
      isSelf: false,
    };
  });

  entries.push({
    nickname: "我",
    score: bestScore,
    isSelf: true,
  });

  return entries
    .sort((a, b) => b.score - a.score)
    .slice(0, config.maxEntries)
    .map((entry, index) => ({ ...entry, rank: index + 1 }));
}

class WorldLeaderboardManager {
  constructor(config) {
    this.config = config;
    this.entries = loadCache();
  }

  getCachedEntries() {
    return this.entries.slice(0, this.config.maxEntries);
  }

  refresh(bestScore) {
    return new Promise((resolve) => {
      if (!this.config.endpoint || !wx.request) {
        const fallback = buildFallbackEntries(this.config, bestScore);
        this.entries = fallback;
        saveCache(fallback);
        resolve({ entries: fallback, source: "fallback" });
        return;
      }

      wx.request({
        url: `${this.config.endpoint}/top`,
        method: "GET",
        data: { limit: this.config.maxEntries },
        timeout: this.config.timeoutMs,
        success: (res) => {
          const list = Array.isArray(res.data && res.data.list) ? res.data.list : [];
          const parsed = list
            .map((row, index) => ({
              rank: row.rank || index + 1,
              nickname: row.nickname || "世界玩家",
              score: Number(row.score) || 0,
              isSelf: Boolean(row.isSelf),
            }))
            .filter((item) => Number.isFinite(item.score));

          const withSelf = this.injectSelf(parsed, bestScore);
          const sorted = withSelf
            .sort((a, b) => b.score - a.score)
            .slice(0, this.config.maxEntries)
            .map((entry, index) => ({ ...entry, rank: index + 1 }));

          this.entries = sorted;
          saveCache(sorted);
          resolve({ entries: sorted, source: "api" });
        },
        fail: () => {
          const fallback = buildFallbackEntries(this.config, bestScore);
          this.entries = fallback;
          saveCache(fallback);
          resolve({ entries: fallback, source: "fallback" });
        },
      });
    });
  }

  submitScore(bestScore) {
    if (!this.config.endpoint || !wx.request) {
      return;
    }

    wx.request({
      url: `${this.config.endpoint}/submit`,
      method: "POST",
      data: { score: bestScore },
      timeout: this.config.timeoutMs,
      success: () => {},
      fail: () => {},
    });
  }

  injectSelf(entries, bestScore) {
    if (entries.some((entry) => entry.isSelf)) {
      return entries;
    }
    return entries.concat([
      {
        nickname: "我",
        score: bestScore,
        isSelf: true,
      },
    ]);
  }
}

module.exports = {
  WorldLeaderboardManager,
};
