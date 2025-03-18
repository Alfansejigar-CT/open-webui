# tasks.py
import asyncio
from typing import Dict
from uuid import uuid4
from open_webui.models.externalResources import ExternalResources

# A dictionary to keep track of active tasks
tasks: Dict[str, asyncio.Task] = {}


def cleanup_task(task_id: str):
    """
    Remove a completed or canceled task from the global `tasks` dictionary.
    """
    tasks.pop(task_id, None)  # Remove the task if it exists


def create_task(coroutine):
    """
    Create a new asyncio task and add it to the global task dictionary.
    """
    task_id = str(uuid4())  # Generate a unique ID for the task
    task = asyncio.create_task(coroutine)  # Create the task

    # Add a done callback for cleanup
    task.add_done_callback(lambda t: cleanup_task(task_id))

    tasks[task_id] = task
    return task_id, task


def get_task(task_id: str):
    """
    Retrieve a task by its task ID.
    """
    return tasks.get(task_id)


def list_tasks():
    """
    List all currently active task IDs.
    """
    return list(tasks.keys())


async def stop_task(task_id: str):
    """
    Cancel a running task and remove it from the global task list.
    """
    task = tasks.get(task_id)
    if not task:
        raise ValueError(f"Task with ID {task_id} not found.")

    task.cancel()  # Request task cancellation
    try:
        await task  # Wait for the task to handle the cancellation
    except asyncio.CancelledError:
        # Task successfully canceled
        tasks.pop(task_id, None)  # Remove it from the dictionary
        return {"status": True, "message": f"Task {task_id} successfully stopped."}

    return {"status": False, "message": f"Failed to stop task {task_id}."}


async def periodic_sync(user_id: str, interval: int = 60):
    """
    Fetch all drive links for the user and sync periodically.
    Runs indefinitely every `interval` seconds until canceled.
    """
    try:
        while True:
            resources_list = ExternalResources.get_resources_by_user_id(user_id)
            print(f"Syncing resources for user {user_id}: {resources_list}")
            
            # Call your sync logic here as needed
            # Example: sync_single_drive_resource(resources_list)

            await asyncio.sleep(interval)  # Wait for the specified interval before running again
    except asyncio.CancelledError:
        print(f"Periodic sync canceled for user {user_id}")
        # Perform any necessary cleanup if needed
        raise