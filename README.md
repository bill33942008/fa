# 金币冲冲冲（UV优先版）

当前版本以**先做用户增长（UV）**为目标：

- 广告接口已完整预留
- 前端默认不展示广告内容
- 复活 / 双倍奖励在公测期走“免费福利”
- 可在配置里一键切到广告变现模式

---

## 1) 当前核心玩法与增长能力

### 核心玩法

- 单指左右滑动，接金币、躲炸弹
- 连击后进入“狂热模式”（得分提升）
- 新增钻石掉落（高分道具）
- 新增任务系统（每局随机任务 + 金币奖励）

### 道具系统

- 护盾：抵挡 1 次炸弹
- 磁铁：自动吸附附近金币
- 慢速：一段时间全场减速

### 社交与竞争

- 好友排行榜：微信好友云数据 + 本地兜底
- 世界排行榜：API 接口预留 + 本地兜底
- 菜单和结算页都可切换“好友榜 / 世界榜”

### 数据看板（内置）

- 会话数（UV参考）
- 局完成率
- D1留存达成状态
- 激励完成率
- ARPU估算值

---

## 2) 项目结构

```text
.
├── game.js
├── game.json
├── project.config.json
└── src
    ├── ad-manager.js
    ├── analytics.js
    ├── app.js
    ├── config.js
    ├── leaderboard.js
    ├── monetization.js
    ├── storage.js
    └── world-leaderboard.js
```

---

## 3) 本地运行

1. 微信开发者工具导入当前目录（小游戏）
2. 保持 `compileType: "game"`
3. 直接预览

> 当前 `appid` 是 `touristappid`，上线前替换成正式小游戏 AppID。

---

## 4) 广告预留与一键开关

### 当前默认状态（冲UV）

`src/config.js`：

```js
monetization: {
  adEnabled: false,
  allowFreeRewardInPrelaunch: true,
  prelaunchTag: "公测冲UV模式",
}
```

效果：

- Banner / 插屏 / 激励视频都不展示
- 复活和双倍奖励依然可用（免费福利），不影响留存
- 前端不会展示广告内容

### 达到 500 UV 后切广告

1. 替换 `AD_UNIT_IDS` 为真实广告位
2. 把 `adEnabled` 改为 `true`

```js
monetization: {
  adEnabled: true,
  allowFreeRewardInPrelaunch: false,
  prelaunchTag: "公测冲UV模式",
}
```

---

## 5) 好友榜与世界榜接入说明

### 好友榜（已可用）

- 读取：`wx.getFriendCloudStorage`
- 上报：`wx.setUserCloudStorage`
- 文件：`src/leaderboard.js`

### 世界榜（接口已预留）

文件：`src/world-leaderboard.js`

预留接口：

- `GET {endpoint}/top?limit=20`
- `POST {endpoint}/submit` body: `{ score }`

在 `src/config.js` 配置：

```js
worldLeaderboard: {
  endpoint: "",
  timeoutMs: 2200,
  maxEntries: 20,
}
```

> `endpoint` 为空时自动走本地兜底榜，不影响前端体验。

---

## 6) 下一步建议（从UV到收入）

1. 先跑 UV：继续做活动和分享拉新
2. 接世界榜服务端：防作弊 + 排名赛季
3. 达标后开启广告：先低频插屏 + 激励复活/双倍
4. 用埋点做 A/B：平衡留存与ARPU
