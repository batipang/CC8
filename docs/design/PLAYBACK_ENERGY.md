# 播报进度方案二：选择、修订与实现

2026-11-01，用户选择三张独立生成图中实际显示的第二张“斜切能量舱”，随后要求跑秒拖尾更简洁、播报字幕缩小至完整三行。当前实现：Android 0.8.6。

- [选定的第二张方案图](playback-energy-selected.png)
- [加入本轮反馈后的修订设计图](playback-energy-three-lines.png)
- 实机验证见[验证记录](../VALIDATION.md)及仓库根目录 design-qa.md；实际截图保留在忽略目录。

设计图使用内置 image_gen 生成。它们是设计参考，示例字幕/任务内容不写入设备数据；实际控件保留既有原生布局锚点和真实回执。小时/分钟仍用长短黄线，跑秒按真实系统时钟，辅助文字不跟随字幕一起缩小。

## 本轮修订的最终生成提示词

输入图一：上面的选定第二张图。输入图二：当时原生界面的本机私有截图，不提交该截图。工具模式：内置 image_gen，非CLI。

```text
Use case ui-mockup / precise-object-edit. Revise the selected FIRST reference image, which is the owner's chosen slanted-energy-cell playback bar. The second image is the actual native CC8 app and anchors layout positions. Create one production-quality 1280x800 landscape CC8 assistant screen, content only with no device frame or side annotations. Keep the approved G1 Bumblebee head identity, navy background, cyan/amber palette, sixty clock ticks, short thick yellow hour mark and long yellow minute mark, separate particle waveform, status BELOW that waveform and all header/footer/task/reminder layout unchanged.
Only apply these explicit owner refinements:
1. Chosen progress remains forward-slanted cyan parallelogram cells, hollow unplayed outlines, cut-corner thin HUD enclosure and amber slanted current-progress gate at 60 percent. It sits at x588 y390 width524 height40, with elapsed time '00:06' outside at the right. Keep it elegant and legible, with controlled cyan light and no huge glow.
2. The spoken caption area x587 y229 width665 height153 must show EXACTLY THREE complete lines, with smaller type than the old oversized caption: about36px bold. Use these exact three lines: '这是一段播报内容。' / '字幕与播放进度保持同步。' / '三行文字完整显示。'. First line amber, the next lines muted blue. No fourth line fragment; do not move any text into the progress bar. Reserve a clear empty gap before the bar.
3. Remove the ugly segmented comet tail on the outer clock. Replace it with one short, smooth slim cyan arc and a small crisp bright point at its leading edge. No stepped pieces, heavy bloom, big rings around the point or long tail. Seconds are clockwise, with the bright leading point at12 o'clock at00 seconds. Keep NO numeric/digital clock anywhere.
Keep other auxiliary fonts at the existing large sizes; only shrink the spoken caption. Neutral mock task text '当前任务 · 暂无任务', status '系统待命'. Current date2026-11-01 Asia/Shanghai; no date needs displaying. Maintain purposeful space, no extra cards or features. This is one refinement of the chosen style, not a new option.
```
