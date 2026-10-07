"""CANoe 17 COM backend. Owns every COM proxy; only contract types leave it.

Importing this package does not import pywin32; the STA worker loads it on
its own thread.
"""
