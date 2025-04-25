# EPICS-MCP-Server
# Overview
The EPICS MCP Server is a Python-based server designed to interact with EPICS (Experimental Physics and Industrial Control System) process variables (PVs). It provides a set of tools to retrieve PV values, set PV values, and fetch detailed information about PVs. The server is built using the mcp framework and communicates over stdio, making it suitable for integration into larger control systems or workflows.

This tool is particularly useful in environments where EPICS PVs are used for monitoring and controlling hardware or software parameters.

# Features
The EPICS MCP Server provides the following tools:

1. Get PV Value
Description: Retrieves the current value of a specified PV.
Tool Name: get_pv_value
Input Parameters:
pv_name (string): The name of the PV variable.
Output:
A JSON object containing the status (success or error) and the retrieved value or an error message.
2. Set PV Value
Description: Sets a new value for a specified PV.
Tool Name: set_pv_value
Input Parameters:
pv_name (string): The name of the PV variable.
pv_value (string): The new value to be set for the PV.
Output:
A JSON object containing the status (success or error) and a confirmation message or an error message.
3. Get PV Information
Description: Fetches detailed information about a specified PV.
Tool Name: get_pv_info
Input Parameters:
pv_name (string): The name of the PV variable.
Output:
A JSON object containing the status (success or error) and the detailed information about the PV or an error message.
