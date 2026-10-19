"""KKBK 新引擎：单次调用的轻层状态机，替换 Maisaka 的规划器/回复器两段式。

本包的纯逻辑模块（config、inbound、triggers、gate、actions、guards、forward、prompt、shadow_log、replay_eval）
只依赖标准库，可以脱离 MaiBot 运行时离线测试；接入 MaiBot 的部分放在单独模块里。
"""
