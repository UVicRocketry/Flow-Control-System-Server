VALVES = {"N2OF", "N2OV", "N2F", "ERV", "MEV", "RTV", "IGPRIME", "NCV"}
ACTION_MAP = {"OPEN": "OPEN", "FILL": "OPEN", "CLOSE": "CLOSE", "VENT": "CLOSE"}

async def send_valve_command(self, valve: str, action: str) -> None:
    valve = valve.strip().upper()
    fw_action = ACTION_MAP.get(action.strip().upper())

    if valve not in VALVES or fw_action is None:
        log.warning("Rejected valve command: %s %s", valve, action)
        await self.ws_server.send_alert("WARN", f"Unsupported command: {valve} {action}")
        return

    await self._cmd_queue.put(f"MCC,CTRL,{valve},{fw_action}\n")


def send_abort(command: str):
