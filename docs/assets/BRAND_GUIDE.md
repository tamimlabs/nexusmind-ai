# NexusMind AI — Brand Assets

Visual identity for the NexusMind AI project: an autonomous, event-driven task-execution agent.

## The mark

The icon reads the product's own mechanic: **six quiet watchers feed one decision core, and a single connection lights up when the agent acts.**

- The hexagon core is the agent's decision loop.
- The six outer nodes are the platform watchers (GitHub, Slack, Jira, Email, RSS, and more).
- Five spokes stay dim — most events don't need a human or a risky action.
- One spoke glows warm coral — the moment the agent actually executes something.

This isn't decorative network iconography; it's a literal diagram of "memory-gated autonomy" from the README, simplified to a mark that still works at 16px.

## Files in this folder

```
assets/
├── logo/
│   ├── nexusmind-dark-full.png        Icon + wordmark, transparent bg, light text — for dark surfaces (README, dark-mode docs)
│   ├── nexusmind-light-full.png       Icon + wordmark, transparent bg, dark text — for light surfaces
│   ├── wordmark-dark.png              Text-only lockup, light text (no icon)
│   ├── wordmark-light.png             Text-only lockup, dark text (no icon)
│   ├── icon-transparent-1024.png      Icon only, transparent background
│   └── icon-monochrome-1024.png       Single-color icon for watermarks/stamps
├── icons/
│   ├── app-icon-*.png                 Icon on a rounded navy card, 16–1024px (avatars, app icons)
│   ├── favicon.ico                    Multi-resolution favicon (16/32/48/64/128/256)
│   ├── favicon-16.png / favicon-32.png
│   ├── apple-touch-icon.png           180×180
│   └── android-chrome-192.png / android-chrome-512.png
├── social/
│   └── social-preview-1280x640.png    GitHub social preview / Open Graph image
└── svg/
    └── *.svg                          Editable vector source for everything above
```

The primary logo also sits at `docs/nexusmind-dark-full.png` — the exact path the README already links to.

## Where to use which file

| Context | File |
|---|---|
| GitHub README (dark card, as already set up) | `logo/nexusmind-dark-full.png` |
| Docs site or anything on a white/light background | `logo/nexusmind-light-full.png` |
| Browser tab | `icons/favicon.ico` |
| iOS home screen / PWA | `icons/apple-touch-icon.png` |
| Android home screen / PWA manifest | `icons/android-chrome-192.png`, `android-chrome-512.png` |
| GitHub repo social preview (Settings → Social preview) | `social/social-preview-1280x640.png` |
| GitHub org/repo avatar, Discord/Slack app icon | `icons/app-icon-512.png` |
| Letterhead, footer, anywhere the icon is redundant | `logo/wordmark-dark.png` or `wordmark-light.png` |
| Watermark on a screenshot or slide | `logo/icon-monochrome-1024.png` |

## Color palette

| Role | Hex | Use |
|---|---|---|
| Background (dark) | `#090C14` | Primary dark surface |
| Card (dark, subtle) | `#0D1220` | Elevated dark surface |
| Background (light) | `#F6F7FB` | Primary light surface |
| Core gradient start | `#5B8CFB` | Icon core |
| Core gradient end | `#7B5CFA` | Icon core |
| Action accent | `#FF6A45` | The single "live" node/spoke — use sparingly, it means "acting now" |
| Quiet node | `#4A5578` | Inactive watchers, dividers |
| Text on dark | `#F4F6FB` | Headings on dark backgrounds |
| Muted text on dark | `#97A1BE` | Body/secondary text on dark |
| Text on light | `#12162B` | Headings on light backgrounds |
| Muted text on light | `#5B6684` | Body/secondary text on light |

The coral accent is intentionally reserved for the one "active" element in the mark. Don't recolor multiple nodes coral — it dilutes the "only escalates when it matters" story the mark is built to tell.

## Typography

- **Space Grotesk (Bold)** — wordmark and display headings.
- **Manrope (Regular / Medium / SemiBold)** — taglines, body copy, the small "AI" badge.

Both are open-source (SIL OFL) Google Fonts. Static instances used to build these assets are included under `../../fonts/` if you need to regenerate anything.

## Usage notes

- Keep clear space around the mark equal to roughly half the icon's width on every side.
- Don't stretch, rotate, or recolor the icon outside the palette above.
- Don't move the coral accent to more than one node at a time.
- The `AI` badge always sits to the right of "NexusMind," never above or below it.
- All PNGs are exported with transparent backgrounds except the app icons and social preview, which carry their own dark card by design — no need to add a container behind them.
