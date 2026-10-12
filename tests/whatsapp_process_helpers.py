"""Spawnable synthetic IPC targets. No requests, production data or database."""

import time


ACK = b'{"outcome":"accepted","provider_message_id":"wamid.synthetic_1=","retry_after_seconds":null}'


def ack_child(pipe, settings, endpoint, report, key):
    try:
        pipe.send_bytes(ACK)
    finally:
        pipe.close()


def stalled_writer(pipe, settings, endpoint, report, key):
    # Readability is not completion. Parent must wait for child exit, then time out.
    pipe.send_bytes(ACK)
    time.sleep(60)
