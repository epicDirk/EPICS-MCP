from enum import Enum
from epics import caget, cainfo, caput
import asyncio
from typing import List, Dict
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from dotenv import load_dotenv
import json
from typing import Sequence
import logging


# Load environment configuration
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)


class EpicsTools(str, Enum):
    GET_PV_VALUE = "get_pv_value"
    SET_PV_VALUE = "set_pv_value"
    GET_PV_INFO = "get_pv_info"


class EpicsServer:
    def get_pv_value(self, pv_name: str) -> Dict[str, str]:
        """
        Get the value of a PV based on its name.
        
        Args:
        pv_name (str): The name of the Process Variable (PV).
        
        Returns:
        Dict[str, str]: A message containing the status and result.
        """
        if not pv_name or not isinstance(pv_name, str):
            return {"status": "error", "message": "PV name cannot be empty and must be a string."}

        logging.info(f"Attempting to get PV value: {pv_name}")
        try:
            value = caget(pv_name, timeout=5)
            if value is None:
                raise TimeoutError("Channel connect timed out")
            logging.info(f"Successfully retrieved PV value: {value}")
            return {"status": "success", "value": value}
        except TimeoutError:
            logging.error(f"Timeout while getting PV '{pv_name}' value")
            return {"status": "error", "message": f"Timeout while getting PV '{pv_name}' value. Please check the network connection."}
        except Exception as e:
            logging.error(f"Error occurred while getting PV '{pv_name}' value: {str(e)}")
            return {"status": "error", "message": f"An unknown error occurred: {str(e)}"}

    def get_pv_info(self, pv_name: str) -> Dict[str, str]:
        """
        Get detailed information about a PV based on its name.
        
        Args:
        pv_name (str): The name of the Process Variable (PV).
        
        Returns:
        Dict[str, str]: A message containing the status and result.
        """
        if not pv_name or not isinstance(pv_name, str):
            return {"status": "error", "message": "PV name cannot be empty and must be a string."}

        logging.info(f"Attempting to get PV info: {pv_name}")
        try:
            info = cainfo(pv_name, print_out=False, timeout=5)
            if info is None:
                raise TimeoutError("Channel connect timed out")
            logging.info(f"Successfully retrieved PV info: {info}")
            return {"status": "success", "info": info}
        except TimeoutError:
            logging.error(f"Timeout while getting PV '{pv_name}' info")
            return {"status": "error", "message": f"Timeout while getting PV '{pv_name}' info. Please check the network connection."}
        except Exception as e:
            logging.error(f"Error occurred while getting PV '{pv_name}' info: {str(e)}")
            return {"status": "error", "message": f"An unknown error occurred: {str(e)}"}

    def set_pv_value(self, pv_name: str, pv_value: str) -> Dict[str, str]:
        """
        Set the value of a PV based on its name.
        
        Args:
        pv_name (str): The name of the Process Variable (PV).
        pv_value (str): The new value to set.
        
        Returns:
        Dict[str, str]: A message containing the status and result.
        """
        if not pv_name or not isinstance(pv_name, str):
            return {"status": "error", "message": "PV name cannot be empty and must be a string."}
        if not pv_value or not isinstance(pv_value, str):
            return {"status": "error", "message": "PV value cannot be empty and must be a string."}

        logging.info(f"Attempting to set PV value: {pv_name} -> {pv_value}")
        try:
            success = caput(pv_name, pv_value, timeout=5)
            if not success:
                raise ValueError("Set operation failed")
            logging.info(f"Successfully set PV value: {pv_name} -> {pv_value}")
            return {"status": "success", "message": f"Successfully set PV '{pv_name}' value to: {pv_value}"}
        except TimeoutError:
            logging.error(f"Timeout while setting PV '{pv_name}' value")
            return {"status": "error", "message": f"Timeout while setting PV '{pv_name}' value. Please check the network connection."}
        except Exception as e:
            logging.error(f"Error occurred while setting PV '{pv_name}' value: {str(e)}")
            return {"status": "error", "message": f"An unknown error occurred: {str(e)}"}


# Add type annotations
server: Server = Server("epics_tools")

epics_server = EpicsServer()


@server.list_tools()
async def handle_list_tools() -> List[Tool]:
    """List available EPICS tools."""
    return [
        Tool(
            name=EpicsTools.GET_PV_VALUE.value,
            description="Get the value of a specific PV.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pv_name": {
                        "type": "string",
                        "description": "The name of the PV variable provided by the user.",
                    }
                },
                "required": ["pv_name"],
            },
        ),
        Tool(
            name=EpicsTools.SET_PV_VALUE.value,
            description="Set the value of a specific PV.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pv_name": {
                        "type": "string",
                        "description": "The name of the PV variable provided by the user."
                    },
                    "pv_value": {
                        "type": "string",
                        "description": "The new PV value provided by the user."
                    },
                },
                "required": ["pv_name", "pv_value"],
            },
        ),
        Tool(
            name=EpicsTools.GET_PV_INFO.value,
            description="Get information about a specific PV.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pv_name": {
                        "type": "string",
                        "description": "The name of the PV variable provided by the user."
                    }
                },
                "required": ["pv_name"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict
) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Handle tool calls for EPICS queries."""
    try:
        match name:
            case EpicsTools.GET_PV_VALUE.value:
                pv_name = arguments.get("pv_name")
                if not pv_name:
                    raise ValueError("Missing required argument: pv_name")
                result = epics_server.get_pv_value(pv_name)

            case EpicsTools.SET_PV_VALUE.value:
                if not all(k in arguments for k in ["pv_name", "pv_value"]):
                    raise ValueError("Missing required arguments")
                result = epics_server.set_pv_value(
                    arguments["pv_name"], arguments["pv_value"]
                )

            case EpicsTools.GET_PV_INFO.value:
                pv_name = arguments.get("pv_name")
                if not pv_name:
                    raise ValueError("Missing required argument: pv_name")
                result = epics_server.get_pv_info(pv_name)

            case _:
                raise ValueError(f"Unknown tool: {name}")

        return [
            TextContent(type="text", text=json.dumps(result, indent=2))
        ]
    except Exception as e:
        raise ValueError(f"Error processing MCP-EPICS query: {str(e)}")


async def main() -> None:
    """Run the MCP server with stdio transport."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="mcp_epics_server",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
