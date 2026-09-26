import print_queue_manager


QueueManager = print_queue_manager.PrintQueueManager()
print(QueueManager.read_gcode("test_prints\\Nosecone_ASA-AERO_15h50m.gcode"))
print(QueueManager.read_gcode("test_prints\\Tail.gcode.3mf"))