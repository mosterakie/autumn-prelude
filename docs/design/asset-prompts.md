# 本次前端素材

用途：秋序首页独立角色立绘。模式：内置 imagegen，新生成，透明背景。

最终文件：frontend/public/images/tingyun-hero.png。这是 AI 生成的角色同人风格插画，非官方素材。

最终提示词：

> Use case: stylized-concept. Asset type: transparent character illustration for Autumn Prelude personal website hero, NOT a website screenshot. Draw Tingyun from Honkai Star Rail, recognizable adult female fox ears, chestnut brown long hair, green eyes, fluffy fox tail, elegant red ivory black and gold Chinese-inspired outfit with jade ornaments, holding a folding fan. Waist-up three-quarter portrait, looking gently toward the viewer, poised and friendly, beautiful detailed anime illustration with delicate ink lines, soft painterly shading, muted warm terracotta burgundy and antique gold palette, cream highlights. All ears, head, fan and tail stay within the canvas with margins, waist can end at bottom. One character only, no letters, no text, no logos, no interface, no scenery. Truly transparent background, high quality clean alpha cutout. Intended as a character-inspired fan illustration, not an official asset.

## 新版聊天像素形象（2026-10-06）

用户要求重新生成停云像素形象，并允许放大显示。模式：内置 imagegen，新生成，透明背景；不修改首页立绘。

最终文件：frontend/public/images/tingyun-pixel.png。原始 PNG 为 1254 × 1254，已检查真实 alpha 通道（含完全透明与完全不透明像素），保留生成结果。角色为全身 Q 版像素风，栗色狐耳和尾巴、绿眼、红金服饰及折扇。

AssistantAvatar 使用该图片替换旧的内嵌 SVG，桌面展示区域 96 × 112 px、手机 72 × 84 px，object-fit 保持比例，image-rendering 保留像素观感；状态文字、思考时轻微浮动、减少动画和收起功能继续保留。

基础检查：前端 TypeScript 通过；独立页面使用正式组件检查图片加载、桌面/手机尺寸、等待确认状态和收起/恢复；390 px 宽度无横向溢出，头像与额度不重叠。截图位于工作区 outputs/tingyun-pixel/desktop.jpg 与 mobile.jpg。未运行生产构建或账户/模型业务流程。

最终提示词：

> Use case: stylized-concept. Asset type: transparent pixel-art character sprite for the chat assistant on the Autumn Prelude personal website. Create a brand-new SINGLE recognizable chibi Tingyun from Honkai: Star Rail, an adult female Foxian in super-deformed proportions, big fluffy chestnut fox ears with cream interiors, chestnut brown long hair, expressive jade-green eyes, a gentle clever smile, one large curled chestnut fox tail with a cream tip. Elegant muted burgundy-red, ivory, dark brown and antique gold Chinese-inspired outfit with small jade accents, holding a small ivory and gold folding fan near her chest. Full body standing, front three-quarter view, large readable face, compact charming silhouette, arms and fan anatomically coherent, entire ears, tail, feet and outfit completely within frame, centered and filling about 85 percent of a square canvas. Style: authentic carefully hand-pixeled 16-bit game sprite, visible uniform square pixel grid approximately 128x128 native pixel scale, crisp stepped outlines, selective dark outlines, limited warm color palette, chunky pixel clusters, 2-3 shade steps per material, no smooth painterly gradients or antialiasing, polished pixel details that remain readable at 96px display size. A genuinely transparent background with clean alpha edges, no ground shadow, no platform, no backdrop, no circle, no sticker white border. One character only, one pose only, no sprite sheet, no lettering, no text, no logos, no watermark, no UI. It is a character-inspired fan illustration, not an official asset.
