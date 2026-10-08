from aiogram.fsm.state import State, StatesGroup

class UploadState(StatesGroup):
    waiting_for_file = State()
    waiting_for_feedback = State()