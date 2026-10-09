from fastapi import APIRouter
from pydantic import BaseModel,Field
from ..optimization import GenerationProfile

class ProfileRequest(BaseModel):
    name:str=Field(min_length=1,max_length=40)
    temperature:float=Field(ge=0,le=2)
    max_tokens:int=Field(ge=16,le=4096)
    context_window:int=Field(ge=2048,le=65536)
    prompt_template:str=Field(min_length=10,max_length=8000)
    quote_hints:bool=False
    max_context_chars:int=Field(default=12000,ge=1000,le=48000)

def optimization_router(lab):
    router=APIRouter(prefix='/api/optimization')
    @router.get('')
    def state():return lab.state()
    @router.post('/profile')
    def apply_profile(body:ProfileRequest):return {'runtime':lab.apply(GenerationProfile(**body.model_dump())),'active':lab.state()['active']}
    @router.post('/compare')
    def compare():return lab.start()
    return router
