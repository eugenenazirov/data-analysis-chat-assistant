from retail_agent.bootstrap import Runtime


def test_runtime_composes_configured_agent_and_adapters(test_config):
    runtime = Runtime(test_config)

    assert runtime.config is test_config
    assert runtime.bigquery.config is test_config
    assert runtime.golden_store.config is test_config
    assert runtime.analysis_agent._function_toolset.tools["run_sql_query"].max_retries == 2
