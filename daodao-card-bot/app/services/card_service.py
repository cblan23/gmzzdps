from typing import Protocol

class CardError(Exception):
    def __init__(self,code):
        self.code=code
        super().__init__(code)

class CardService(Protocol):
    async def claim(self,qq,group_id,request_id): ...
    async def stats(self,admin_qq): ...
