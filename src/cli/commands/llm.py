import asyncio
import click
from src.cli import cli
from src.config import load_config, DiarySinkConfig
from src.sinks.diary import DiaryConfig, llm_merge

@cli.group()
def llm():
    """LLM related commands."""
    pass

@llm.command("test")
@click.option("--sink", help="Specific diary sink name to use for configuration. If not provided, the first diary sink found will be used.")
def test_llm(sink: str):
    """Verify that the LLM configuration is correct and works."""
    try:
        config = load_config()
    except Exception as e:
        click.secho(f"Failed to load configuration: {e}", fg="red")
        return

    diary_sink_config = None
    
    if sink:
        if sink not in config.sink:
            click.secho(f"Sink '{sink}' not found in configuration.", fg="red")
            return
        if not isinstance(config.sink[sink], DiarySinkConfig):
            click.secho(f"Sink '{sink}' is not a diary sink.", fg="red")
            return
        diary_sink_config = config.sink[sink]
    else:
        # Find the first diary sink
        for name, s_cfg in config.sink.items():
            if isinstance(s_cfg, DiarySinkConfig):
                diary_sink_config = s_cfg
                sink = name
                break
    
    if not diary_sink_config:
        click.secho("No diary sink found in configuration. LLM configuration is currently tied to diary sinks.", fg="yellow")
        click.echo("Using environment variables or defaults for testing...")
        # Create a dummy config to hold env vars/defaults
        diary_sink_config = DiarySinkConfig(path="dummy")

    try:
        # DiaryConfig.from_sink_config expects the Pydantic model
        llm_config = DiaryConfig.from_sink_config(diary_sink_config)
    except Exception as e:
        click.secho(f"Failed to initialize LLM configuration: {e}", fg="red")
        return

    click.echo(f"Testing LLM configuration (sink: {sink or 'default/env'})...")
    click.echo(f"Endpoint: {llm_config.llm_endpoint_url or 'OpenAI Default'}")
    click.echo(f"Model: {llm_config.llm_model}")
    click.echo(f"API Key: {'set' if llm_config.llm_api_key else 'NOT SET'}")

    if not llm_config.llm_api_key:
        click.secho("Error: LLM API key is not set. Set DIARY_LLM_API_KEY or OPENAI_API_KEY.", fg="red")
        return

    if not llm_config.llm_model:
        click.secho("Error: LLM model is not set. Set DIARY_LLM_MODEL or OPENAI_MODEL.", fg="red")
        return

    prompt = "You are a connectivity test. Respond with exactly the word 'OK' and nothing else."
    input_artifact = "Test signal"

    click.echo("Sending test prompt...")
    try:
        result = asyncio.run(llm_merge(llm_config, prompt, input_artifact))
        click.echo(f"Response: {result}")
        if "OK" in result.upper():
            click.secho("LLM test successful!", fg="green")
        else:
            click.secho("LLM responded, but the output was unexpected.", fg="yellow")
    except Exception as e:
        click.secho(f"LLM test failed: {e}", fg="red")
        if "openai" in str(e).lower():
            click.echo("Make sure the 'openai' package is installed: pip install openai")
