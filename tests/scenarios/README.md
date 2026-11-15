# 后续场景接入

各阶段在此增加 `test_<module>.py`，复用 `tests/conftest.py` 的 bundle、scenarios、workspace 夹具。

`python scripts/project.py scenario` 运行无界面场景；`python scripts/project.py check` 同时执行单元、契约和场景检查。检查失败会返回非零码。

P2增加规划/独立复核场景；P3增加调度与人工关口；P4增加真实执行/验收；P5增加恢复/接口；P6补浏览器联调。禁止用跳过检查替代必要验收。

