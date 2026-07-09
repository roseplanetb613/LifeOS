# 图像转六边形粒子画 — 设计规格

## 目标
加载一张图片，将其像素转为发光的六边形粒子阵列。粒子以蜂巢排列悬浮在世界空间中，每个粒子继承对应像素的颜色，核心发光白亮，边缘裁剪为六边形。

## 文件清单

```
Assets/Scripts/ImageParticleSystem.cs      ← 主控 MonoBehavior
Assets/Art/Shaders/ParticleHexagon.shader  ← 六边形粒子 Shader
Assets/Art/Materials/ParticleHexagon.mat   ← 材质
```

## Inspector 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| sourceImage | Texture2D | null | 源图片（需开启 Read/Write Enabled） |
| resolution | enum (64/128/256/512) | 256 | 采样网格精度 |
| spacing | float (0~2) | 0.5 | 0=紧密拼接, 1=标准间隙, 2=极稀疏 |
| particleSize | float (0.01~0.5) | 0.03 | 单个六边形世界空间半径 |
| glowStrength | float (0~1) | 0.6 | 核心发光强度 |
| particleMaterial | Material | null | 使用 ParticleHexagon.shader 的材质 |

## CPU 端流程

1. Awake: 代码生成 quad mesh，校验参数，创建 Indirect args buffer
2. Start → RebuildParticles: GetPixels 采样 → 构建 position[] + color[] → 上传 ComputeBuffer → 设置 instance count
3. Update:DrawMeshInstancedIndirect 一次渲染所有粒子

## GPU 端流程 (Shader)

- Vertex: StructuredBuffer 读位置/颜色 → Billboard → 输出 UV
- Fragment: 六边形裁剪 → 核心发光 → 颜色过渡 → 边框线 → 半透明输出

## 布局算法

- 列数 = resolution，行数 = resolution * (图高/图宽)
- 奇数行偏移半个单位形成蜂巢感
- 世界空间总尺寸 = resolution * particleSize * spacing

## 错误处理

- sourceImage 为空 → LogError + 禁用
- 纹理未开启 Read/Write → LogError
- material 为空 → LogError + 禁用
