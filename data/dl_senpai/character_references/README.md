# 角色参考图库

放「确认过」的角色官方立绘，生图时优先用图生图，比纯文本提示词稳得多。

目录约定：

    <作品英文>/<角色英文>/
      ref_01.png
      ref_02.png

例如：

    arknights/eyjafjalla/ref_01.png
    arknights/eyjafjalla/ref_02.png
    arknights/kaltsit/ref_01.png

说明：

- 目录名取 `CharacterProfile.slug`（作品小写去空格 + 角色小写去空格），
  可在调试日志的 character 字段里核对。
- 只读不写：程序**不会**把网上搜到的图偷偷存进来——那等于用未核实的图污染参考库。
- 支持 .png / .jpg / .jpeg，单张上限 8MB；会校验文件头，改名过的非图片文件会被跳过。
- 同一个角色按文件名排序取第一张（所以 ref_00/ref_01 是最优先的）。
