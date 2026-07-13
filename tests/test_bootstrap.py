from retail_agent.agent import build_analysis_toolset
from retail_agent.bootstrap import Runtime


def test_runtime_composes_configured_agent_and_adapters(test_config):
    runtime = Runtime(test_config)

    assert runtime.config is test_config
    assert runtime.bigquery.config is test_config
    assert runtime.golden_store.config is test_config
    assert runtime.chart_executor.settings is test_config.chart_execution
    toolset = build_analysis_toolset(test_config)
    assert runtime.analysis_agent is not None
    assert toolset.tools["run_sql_query"].max_retries == 2
