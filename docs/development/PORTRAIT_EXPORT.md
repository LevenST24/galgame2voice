# 立绘坐标与导出

网页收到的是已经合成的完整 PNG/WebP，不能通过移动整个图片修正里面的眼睛和嘴巴。
同一服装的表情应共享身体图层的画布，每个表情图层使用自己的 `left`、`top` 和尺寸。
合成完全部图层后再统一缩放；不要先裁掉各表情的透明边缘再放到固定脸部坐标。
前发覆盖层必须在表情上方，并且与当前服装及姿态相符。

`scripts/tools/compose_portrait_layers.py` 可从原始坐标表重新导出。素材制作需要 Pillow，
正常运行程序无需安装它。坐标表按姿态保存 `layers`，每个图层包含 `layer_id`、
`z_order` 和 `hd: {left, top, width, height}`；切片文件名为 `姿态名_图层编号.png`。
工具先核对全部切片尺寸，拒绝混用不同分辨率，按 `z_order` 合成，并输出坐标与源文件哈希记录。

导出计划示例（编号需要替换成自己的原始图层编号）：

```json
{
  "costumes": [{
    "id": "uniform",
    "pose": "pose_a",
    "frame_layer": 10,
    "sprites": {
      "gentle": [10, 20, 30],
      "happy": [10, 21, 30]
    }
  }]
}
```

这里 `10` 是身体和共享画布，`20`/`21` 是不同表情，`30` 是前发覆盖图层。
输出目录必须不存在，工具不会直接覆盖安装好的角色包。

```powershell
python scripts/tools/compose_portrait_layers.py --coordinates coordinates.json --plan plan.json --slices slices --output .local/verification/portrait-export
```

安装前备份角色包中的整个 `portrait/`，放大检查所有表情后再替换。
保留 `composition.json`，并将 `manifest.json` 或 `expressions.json` 中的宽高与导出结果对应。
角色包中的图片仍是用户素材，单独分发；不随程序仓库或普通便携包发布。

图片接口的 URL 带有文件版本参数。重新导出后，新加载的角色清单会使用新的 URL，
避免浏览器和预加载逻辑继续沿用同名旧 PNG。已经打开的网页需要重新加载角色或刷新页面。
