# This example requires the 'message_content' intent.

import asyncio
from time import sleep

import discord
import os
from print_sub import PrintSubmission
import print_queue_manager
import printer_manager

DOWNLOAD_FOLDER = "C:\\Users\\fx9gaming\\VSCode\\3D-Printer-Manager-Bot\\.downloads\\"
PRINT_SUBMISSION_CHANNEL = "forum-bot-testing"


class MyClient(discord.Client):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.print_manager = print_queue_manager.PrintQueueManager()
        self.printer_manager = printer_manager.PrinterManager()

    async def on_ready(self):
        print(f'Logged on as {self.user}!')
        self.printer_manager.init_H2D(None, None, None)
        self.printer_manager.init_A1mini(None, None, None)
        await self.update_status_message("H2D", "Bot is starting up...")
        await self.update_status_message("A1mini", "Bot is starting up...")

    def get_status_channel(self):
        for channel in self.get_all_channels():
            if channel.name == "3d-printer-status":
                return channel

    async def get_status_message(self, printer_name):
        status_channel = self.get_status_channel()
        if status_channel is None:
            print("Status channel not found.")
            return None

        async def fetch_message():
            async for message in status_channel.history(limit=100):
                if message.author == self.user and message.content.startswith(f"**{printer_name}**"):
                    return message
            return None

        return await fetch_message()

    async def update_status_message(self, printer_name, status_text):
        status_channel = self.get_status_channel()
        if status_channel is None:
            print("Status channel not found.")
            return

        async def update_message():
            message = await self.get_status_message(printer_name)
            if message:
                await message.edit(content=f"**{printer_name}**\n{status_text}")
            else:
                await status_channel.send(f"**{printer_name}**\n{status_text}")

        await update_message()

    async def on_thread_create(self, thread):
        print(f'Thread created: {thread.name}')
        print(f'Thread Tags: {thread.applied_tags}')
        self.print_manager.add_empty_print(thread)

    async def on_message(self, message):
        # Check if the message is in a public thread (forum post)
        if message.channel.type == discord.ChannelType.public_thread and message.channel.parent.name == PRINT_SUBMISSION_CHANNEL:
            # Ensure the bot doesn't respond to its own messages
            if message.author == self.user:
                return

            print(f'Message in thread: {message.channel.name}')
            print(f'Message content: {message.content}')
            print(f'Message has attachments: {len(message.attachments)}')
            for attachment in message.attachments:
                file_path = f'{DOWNLOAD_FOLDER}{attachment.filename}'
                await attachment.save(file_path)
                print(f'Downloaded attachment to (thread_create): {file_path}')
                self.print_manager.add_print(message.channel, file_path)
                await message.channel.send(f"Print added to queue: {attachment.filename}")

    async def update_printers_status(self):
        await self.wait_until_ready()

        while not self.is_closed():
            for printer in self.printer_manager.printers:
                status_text = printer.get_status()
                await self.update_status_message(printer.name, status_text)
            await asyncio.sleep(60)  # Update every 60 seconds

    async def setup_hook(self) -> None:
        # create the background task and run it in the background
        self.bg_task = self.loop.create_task(self.update_printers_status())
        


intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

print(os.environ)
client = MyClient(intents=intents)
client.run(os.environ['BOT_TOKEN'])
