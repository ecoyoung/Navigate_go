# navigate Brand Assets

**Product**: navigate  
**Concept**: 资讯聚合 + 极简阅读 + 日报精选 · 在信息海洋中导航

## 1. Logo

### Graphic Mark（图形标）
- Solid geometric **N** — bold, no thin lines, favicon-safe
- Colors:
  - Black `#000000`
  - White `#FFFFFF`
  - Wine `#722F37`
- Sizes: 24×24 & 1024×1024 SVG

Files:
```
logo/mark-black-24.svg
logo/mark-black-1024.svg
logo/mark-white-24.svg
logo/mark-white-1024.svg
logo/mark-wine-24.svg
logo/mark-wine-1024.svg
```

### Wordmark（横版字标）
- Font: Inter SemiBold (system fallback)
- Light background → dark text `#111111`
- Dark background → white text `#FFFFFF`
- Heights: 32 & 128

Files:
```
logo/wordmark-light-bg-32.svg
logo/wordmark-light-bg-128.svg
logo/wordmark-dark-bg-32.svg
logo/wordmark-dark-bg-128.svg
```

### Lockup (bonus)
```
logo/lockup-light-bg-32.svg
logo/lockup-dark-bg-32.svg
```

> For production print / high-fidelity, convert text to outlines in design tool if needed.

## 2. App Icon

- `app-icon/app-icon-1024.png`
- 1024×1024
- Wine red background `#722F37` + white mark
- iOS continuous corner radius ≈ 22.3% applied
- Same graphic form as logo mark

You can export 32 / 16 favicon from this or from the 24 SVG mark.

## 3. Icons (12)

All: 24×24 viewBox · stroke 1.75px · round caps & joins · monochrome · `currentColor` · no fill (except small centers)

| File | Meaning |
|------|---------|
| featured.svg | 精选 |
| topics.svg | 主题 |
| explore.svg | 探索 |
| daily.svg | 日报 |
| bookmark.svg | 收藏 |
| account.svg | 账号 |
| new.svg | 新建 |
| search.svg | 搜索 |
| source.svg | 来源 |
| delete.svg | 删除 |
| settings.svg | 设置 |
| external.svg | 外链 |

Usage: set `stroke` or use CSS `color` / `currentColor`. Works on both light and dark backgrounds.

---

**Design notes**
- Moved completely away from blue square.
- Mark prioritizes legibility at 16–24px.
- Wine red chosen as classic deep brand accent matching “精选 / 日报” quality feel.
- Icons follow consistent optical weight and corner rounding.
