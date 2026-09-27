import asyncio
from src.bambu_printer import BambuPrinter

async def main():
    printer = BambuPrinter(
        name="H2D-Printer",
        printer_ip="192.168.0.100",  # Double check IP address
        access_code="6ef79e8d",     # Double check Access Code on printer screen
        serial="0948AD540900932"    # Double check Serial Number
    )
    
    print("Connecting...")
    await printer.connect()

    # Wait for MQTT loop
    for i in range(10):
        await asyncio.sleep(1)
        connected = getattr(printer.printer, "_connected", False)
        print(f"[{i+1}s] MQTT Connected State: {connected}")
        if connected:
            break
    
    # REQUIRED: Wait 3-5 seconds for MQTT telemetry payload to arrive
    print("Waiting for telemetry handshake...")
    await asyncio.sleep(4)
    
    # Retrieve status
    status = await printer.get_status()
    print("--- STATUS ---")
    print(status)
    
    # Retrieve camera image
    print("Capturing image...")
    await printer.save_image()
    
    await printer.disconnect()

if __name__ == "__main__":
    asyncio.run(main())