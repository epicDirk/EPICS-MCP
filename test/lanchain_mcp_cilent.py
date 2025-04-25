from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.prebuilt import create_react_agent
from langchain_openai import AzureChatOpenAI
import os
from dotenv import load_dotenv
import asyncio
env_path = ".env"  
load_dotenv(env_path)

azure_openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
azure_openai_api_key = os.getenv("AZURE_OPENAI_API_KEY")
model = AzureChatOpenAI(
    azure_deployment="gpt-4o-mini-2",  
    api_version="2024-02-01",  
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=2,
)

server_params = StdioServerParameters(
    command="python",
    # Make sure to update to the full absolute path to your math_server.py file
    args=["/path/src/epics-mcp-sever/server.py"],
)

async def run():
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the connection
            await session.initialize()

            # Get tools
            tools = await load_mcp_tools(session)

            # Create and run the agent
            agent = create_react_agent(model, tools)
            agent_response = await agent.ainvoke({"messages": "To query the value of a PV (Process Variable) named temperature:water"})
            return agent_response
        

if __name__  == "__main__":
    response = asyncio.run(run())
    for m in response["messages"]:
        m.pretty_print()
