# 09 历史基础检查证据索引

从本地已保存的 JUnit XML 读取；本次整理没有重新运行这些业务测试。
下表保留首次失败及复验，不合并重复用例、不把多次检查的数量相加作为覆盖率。
仅导出用例名称和结果计数，原异常正文、stdout、路径属性及私人运行记录未收入归档。
时间字段仅作为原报告信息保存，没有将执行主机时区当作用户时间；归档日期采用 Asia/Shanghai。

| 原报告                                | 用例 | 失败 | 错误 | 跳过 |
| ------------------------------------- | ---: | ---: | ---: | ---: |
| `pytest-agent-content-basic.xml`      |   14 |    0 |    0 |    0 |
| `pytest-agent-creation-basic.xml`     |    4 |    0 |    0 |    0 |
| `pytest-d.xml`                        |  816 |    0 |    0 |    0 |
| `pytest-e-stage.xml`                  |   31 |    0 |    0 |    0 |
| `pytest-e2.xml`                       |    4 |    0 |    0 |    0 |
| `pytest-e3.xml`                       |    4 |    0 |    0 |    0 |
| `pytest-e5.xml`                       |    3 |    0 |    0 |    0 |
| `pytest-e6.xml`                       |    4 |    0 |    0 |    0 |
| `pytest-e7.xml`                       |    4 |    0 |    0 |    0 |
| `pytest-e8.xml`                       |    5 |    0 |    0 |    0 |
| `pytest-email-basic.xml`              |    8 |    0 |    0 |    0 |
| `pytest-f2.xml`                       |   10 |    0 |    0 |    0 |
| `pytest-f3.xml`                       |   12 |    0 |    0 |    0 |
| `pytest-f4.xml`                       |    5 |    0 |    0 |    0 |
| `pytest-f5.xml`                       |    3 |    0 |    0 |    0 |
| `pytest-f6.xml`                       |    3 |    0 |    0 |    0 |
| `pytest-f7.xml`                       |    3 |    0 |    0 |    0 |
| `pytest-f8-review.xml`                |   18 |    0 |    0 |    0 |
| `pytest-f8.xml`                       |    4 |    0 |    0 |    0 |
| `pytest-g-basic-final.xml`            |   20 |    0 |    0 |    0 |
| `pytest-g1.xml`                       |    1 |    0 |    0 |    0 |
| `pytest-g2.xml`                       |    2 |    0 |    0 |    0 |
| `pytest-g3.xml`                       |    2 |    0 |    0 |    0 |
| `pytest-g4.xml`                       |    1 |    0 |    0 |    0 |
| `pytest-g5.xml`                       |    1 |    0 |    0 |    0 |
| `pytest-g6.xml`                       |    1 |    0 |    0 |    0 |
| `pytest-g7-action.xml`                |    1 |    0 |    0 |    0 |
| `pytest-g7-checkpoint-resume.xml`     |    1 |    0 |    0 |    0 |
| `pytest-g7.xml`                       |    5 |    0 |    0 |    0 |
| `pytest-h-basic.xml`                  |   19 |    0 |    0 |    0 |
| `pytest-h1.xml`                       |    5 |    0 |    0 |    0 |
| `pytest-h2.xml`                       |    2 |    0 |    0 |    0 |
| `pytest-h3.xml`                       |    1 |    0 |    0 |    0 |
| `pytest-h4.xml`                       |    1 |    0 |    0 |    0 |
| `pytest-h5-actions.xml`               |    1 |    0 |    0 |    0 |
| `pytest-h5.xml`                       |    2 |    1 |    0 |    0 |
| `pytest-h6.xml`                       |    1 |    0 |    0 |    0 |
| `pytest-h7.xml`                       |    1 |    0 |    0 |    0 |
| `pytest-h8.xml`                       |    3 |    0 |    0 |    0 |
| `pytest-input-choice-basic.xml`       |    7 |    0 |    0 |    0 |
| `pytest-providers-basic.xml`          |   10 |    0 |    0 |    0 |
| `pytest-providers-live-knowledge.xml` |    1 |    0 |    0 |    0 |
| `pytest-providers-live-web.xml`       |    1 |    0 |    0 |    0 |
| `pytest-providers-live.xml`           |    2 |    1 |    0 |    0 |
| `pytest.xml`                          |  662 |    0 |    0 |    0 |

逐项用例结果见 [evidence-summary.json](evidence-summary.json)。

完整证明仍需绑定当时的代码版本、配置、独立数据库和具体测试范围。历史全量绿灯不能代替归档版本的全量回归，真实供应商复验也不能代替浏览器和邮件验收。
