# 变更

## 0.2.0

- 引入 Handsy `5a4fddf` 的 `hid_bridge`，N16R8 的 FQBN 固定为 `CDCOnBoot=default`、`FlashSize=16M`、`PSRAM=opi`、`PartitionScheme=app3M_fat9M_16MB`。
- 电脑端从 Kz-Aven `5e7b4c2` 改来：串口控制，TCP 7201 + PyAV 画面，不再依赖 UxPlay。
- 默认网络改为由 COM 口写入 NVS 的 STA。AP 仅作有网线时的选项。
- 增加批量动作、skill 坐标、系统熔断。微信群回复放在 skill 里，全自动，无人工审批。
- 固件目录 GPL-3.0，电脑端 MIT。

尚未在真 iPhone 或真 N16R8 上运行过。
