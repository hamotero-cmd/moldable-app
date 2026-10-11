import re, json, io, os
from pathlib import Path
from datetime import datetime
from typing import Optional, List
import fitz, qrcode
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

BASE_DIR = Path(__file__).parent
STORAGE_DIR = BASE_DIR / "storage"
STORAGE_DIR.mkdir(exist_ok=True)
(Storage_DIR := STORAGE_DIR / "sellos").mkdir(exist_ok=True)

ROWS_Y = [
    (241.03, 262.72, 234.10), (289.09, 310.79, 280.74),
    (338.57, 360.27, 329.51), (388.76, 410.46, 378.28),
    (436.11, 457.81, 427.05), (485.63, 507.32, 475.15),
    (533.69, 555.39, 523.92), (584.07, 605.77, 573.60),
    (632.84, 654.54, 623.08),
]
QR_RECT = fitz.Rect(5, 2, 60, 57)
MOLDABLE_RECT = fitz.Rect(65, 8, 200, 45)

app = FastAPI(title="DeCA Carta Porte MOLDABLE")

@app.get("/health")
async def health():
    return {"status":"ok","qr":[5,2,60,57],"moldable":[65,8,200,45]}

@app.get("/", response_class=HTMLResponse)
async def root_health():
    return HTMLResponse('ok - <a href="/docs">docs</a>')

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
if not Path(templates.directory).exists():
    templates.directory = "/mnt/data/carta_porte_pro/templates"

def list_sellos():
    return []

def sanitize_filename(s):
    s = re.sub(r'[\\/:*?"<>|\s]+','_', str(s).strip() or "TEST")
    return s[:60].replace("..","_")

def safe_storage_path(name: str) -> Path:
    clean = Path(name).name
    p = (STORAGE_DIR / clean).resolve()
    if not str(p).startswith(str(STORAGE_DIR.resolve())):
        raise HTTPException(400,"Nombre no valido")
    if not p.exists():
        raise HTTPException(404,"No existe")
    return p

def is_salida(pdf_path: Path):
    return (pdf_path.parent / (pdf_path.stem + '.salida')).exists()

def list_recent():
    files = sorted(STORAGE_DIR.glob("CP_*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)[:15]
    res=[]
    for f in files:
        res.append({"name":f.name,"salida":is_salida(f),"mtime":datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m %H:%M"),"stem":f.stem})
    return res

class VehiculoRow(BaseModel):
    mat: str = ""
    marca: str = ""
    peso: Optional[int] = None
    obs: str = ""
    @field_validator('mat')
    @classmethod
    def clean_mat(cls,v):
        if not v: return ""
        v = re.sub(r'[\s\-]+','', str(v).upper())
        if not re.match(r'^(\d{4}[A-Z]{3}|[A-Z]{1,2}\d{4}[A-Z]{0,2}|[A-HJ-NPR-Z0-9]{17})?$', v, re.I):
            raise ValueError(f'Matricula no valida: {v}')
        return v

class CartaPorte(BaseModel):
    numero: str
    cliente: str
    cif: str
    origen: str = ""
    origen_dir: str = ""
    origen_pob: str = ""
    destino: str = ""
    destino_dir: str = ""
    destino_pob: str = ""
    vehiculos: List[VehiculoRow]
    @field_validator('numero','cliente','cif')
    @classmethod
    def not_empty(cls,v):
        if not str(v).strip(): raise ValueError('Obligatorio')
        return str(v).strip()

def clean_text(page, outer):
    inner = fitz.Rect(outer.x0+2, outer.y0+1.2, outer.x1-2, outer.y1-1.2)
    page.draw_rect(inner,color=(1,1,1),fill=(1,1,1),overlay=True)
def clean_area(page, rect):
    page.draw_rect(rect,color=(1,1,1),fill=(1,1,1),overlay=True)
def write_centered(page,x0,x1,y,text,fs=8.5,bold=False):
    if not text: return
    t=str(text).strip()
    if not t: return
    if len(t)>24: fs=max(fs-1.5,6.5)
    fn="hebo" if bold else "helv"
    try: w=fitz.Font(fn).text_length(t,fontsize=fs)
    except: w=len(t)*fs*0.52
    x=((x0+x1)/2)-(w/2)
    page.insert_text((x,y),t,fontsize=fs,fontname=fn,overlay=True)
def write_obs(page,x0,y0,x1,y1,obs_text,peso_text):
    if not obs_text and not peso_text: return
    r_total=fitz.Rect(x0+5,y0+1,x1-5,y1-3)
    clean_area(page,r_total)
    if obs_text:
        r_obs=fitz.Rect(x0+5,y0+2,x1-5,y1-13)
        fs=6.5 if len(str(obs_text))<60 else 5.5
        page.insert_textbox(r_obs,str(obs_text).strip(),fontsize=fs,fontname="helv",align=0,overlay=True)
    if peso_text:
        r_peso=fitz.Rect(x1-72,y1-11,x1-4,y1-2)
        page.draw_rect(r_peso,color=(0,0,0),fill=(0.95,0.95,0.95),width=0.5,overlay=True)
        page.insert_text((r_peso.x0+3,r_peso.y0+6.5),f"PESO: {peso_text} kg",fontsize=6.5,fontname="helv",overlay=True)

def build_payload(carta: CartaPorte):
    veh=[{"mat":v.mat,"marca":v.marca,"obs":v.obs,"peso":v.peso} for v in carta.vehiculos if v.mat or v.marca]
    payload={"t":"DECA","v":"2","n":carta.numero,"cif":carta.cif,"cli":carta.cliente,"f":datetime.now().isoformat(timespec="seconds"),"veh":veh,"ori":carta.origen,"des":carta.destino}
    return json.dumps(payload,ensure_ascii=False,separators=(",",":"))

def generar_pdf(carta: CartaPorte, raw_data: dict) -> Path:
    tpl = BASE_DIR / "templates" / "carta_porte_original.pdf"
    if not tpl.exists():
        tpl = Path("templates/carta_porte_original.pdf")
    if not tpl.exists():
        raise HTTPException(500,f"Plantilla no encontrada en {tpl}")
    doc=fitz.open(tpl)
    try:
        page=doc[0]
        if carta.numero:
            clean_text(page,fitz.Rect(450,30,560,52))
            page.insert_text((485,42),carta.numero.upper(),fontsize=11,fontname="hebo",overlay=True)
        if carta.cliente:
            clean_text(page,fitz.Rect(80,108,310,124))
            page.insert_text((80,120),carta.cliente,fontsize=9,fontname="helv",overlay=True)
        if carta.cif:
            clean_text(page,fitz.Rect(355,108,490,124))
            page.insert_text((360,120),carta.cif,fontsize=9,fontname="helv",overlay=True)
        if carta.origen:
            clean_text(page,fitz.Rect(48,160,322,180))
            page.insert_textbox(fitz.Rect(82,161,318,179),carta.origen,fontsize=8,fontname="helv",align=0,overlay=True)
        if carta.origen_dir:
            clean_text(page,fitz.Rect(48,180,322,200))
            page.insert_textbox(fitz.Rect(82,181,318,199),carta.origen_dir,fontsize=8,fontname="helv",align=0,overlay=True)
        if carta.origen_pob:
            clean_text(page,fitz.Rect(48,200,322,218))
            page.insert_textbox(fitz.Rect(82,201,318,217),carta.origen_pob,fontsize=8,fontname="helv",align=0,overlay=True)
        if carta.destino:
            clean_text(page,fitz.Rect(330,160,570,180))
            page.insert_textbox(fitz.Rect(395,161,566,179),carta.destino,fontsize=8,fontname="helv",align=0,overlay=True)
        if carta.destino_dir:
            clean_text(page,fitz.Rect(330,180,570,200))
            page.insert_textbox(fitz.Rect(395,181,566,199),carta.destino_dir,fontsize=8,fontname="helv",align=0,overlay=True)
        if carta.destino_pob:
            clean_text(page,fitz.Rect(330,200,570,218))
            page.insert_textbox(fitz.Rect(395,201,566,217),carta.destino_pob,fontsize=8,fontname="helv",align=0,overlay=True)
        for i,(ym,ymod,yo) in enumerate(ROWS_Y, start=1):
            v=carta.vehiculos[i-1] if i-1 < len(carta.vehiculos) else VehiculoRow()
            if not (v.mat or v.marca or v.obs or v.peso): continue
            if v.mat:
                clean_area(page,fitz.Rect(115,ym-6,312,ym+10))
                write_centered(page,115,312,ym+4,v.mat,fs=9,bold=True)
            if v.marca:
                clean_area(page,fitz.Rect(115,ymod-6,312,ymod+10))
                write_centered(page,115,312,ymod+4,v.marca,fs=8)
            if v.obs or v.peso:
                write_obs(page,370,yo+11,575,yo+40,v.obs,str(v.peso or ""))
        payload=build_payload(carta)
        qr=qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=10,border=1)
        qr.add_data(payload)
        qr.make(fit=True)
        img=qr.make_image(fill_color="black",back_color="white")
        buf=io.BytesIO()
        img.save(buf,format="PNG")
        clean_area(page,QR_RECT)
        page.insert_image(QR_RECT,stream=buf.getvalue())
        clean_area(page,MOLDABLE_RECT)
        page.insert_textbox(MOLDABLE_RECT,"MOLDABLE SL",fontsize=8,fontname="hebo",align=0,overlay=True)
        out=STORAGE_DIR / f"CP_{sanitize_filename(carta.numero)}_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        doc.save(out,garbage=4,deflate=True,clean=True)
        with open(out.with_suffix('.json'),'w',encoding='utf-8') as jf:
            json.dump(raw_data,jf,ensure_ascii=False,indent=2)
        return out
    finally:
        doc.close()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html",{"request":request,"prefill":None,"recientes":list_recent(),"rows":range(1,10),"sellos":list_sellos()})

@app.get("/editar/{name}", response_class=HTMLResponse)
async def editar(request: Request, name: str):
    p = safe_storage_path(Path(name).stem + ".json")
    with open(p,'r',encoding='utf-8') as f:
        data=json.load(f)
    return templates.TemplateResponse("index.html",{"request":request,"prefill":data,"recientes":list_recent(),"rows":range(1,10),"sellos":list_sellos()})

@app.post("/generar", response_class=HTMLResponse)
async def generar(request: Request, numero: str = Form(...), cliente: str = Form(...), cif: str = Form(...), origen: str = Form(""), origen_dir: str = Form(""), origen_pob: str = Form(""), destino: str = Form(""), destino_dir: str = Form(""), destino_pob: str = Form("")):
    form=await request.form()
    raw={k:str(v).strip() for k,v in form.items()}
    vehiculos=[]
    for i in range(1,10):
        mat=raw.get(f"mat_{i}","")
        marca=raw.get(f"marca_{i}","")
        peso_raw=raw.get(f"peso_{i}","")
        obs=raw.get(f"obs_{i}","")
        if not (mat or marca or peso_raw or obs):
            vehiculos.append(VehiculoRow(mat="",marca="",peso=None,obs=""))
            continue
        try:
            peso=int(peso_raw) if peso_raw else None
        except:
            raise HTTPException(400,f"Fila {i}: peso debe ser numero")
        if (mat or marca) and not peso:
            raise HTTPException(400,f"Fila {i}: PESO obligatorio")
        try:
            vehiculos.append(VehiculoRow(mat=mat,marca=marca,peso=peso,obs=obs))
        except Exception as e:
            raise HTTPException(400,f"Fila {i}: {e}")
    try:
        carta=CartaPorte(numero=numero,cliente=cliente,cif=cif,origen=origen,origen_dir=origen_dir,origen_pob=origen_pob,destino=destino,destino_dir=destino_dir,destino_pob=destino_pob,vehiculos=vehiculos)
    except Exception as e:
        raise HTTPException(400,f"Error validacion: {e}")
    out=generar_pdf(carta,raw)
    return templates.TemplateResponse("preview.html",{"request":request,"pdf_name":out.name,"file_id":out.stem,"qr_url":f"/r/{out.stem}","qr_int_url":f"/r/{out.stem}"})

@app.get("/salida/{name}")
async def salida(name: str):
    pdf=safe_storage_path(name)
    marker=pdf.parent / (pdf.stem + '.salida')
    marker.write_text(datetime.now().isoformat(),encoding='utf-8')
    return RedirectResponse(url="/",status_code=303)

@app.get("/pdf/view/{name}")
async def view_pdf(name: str):
    path=safe_storage_path(name)
    return FileResponse(path,media_type="application/pdf",headers={"Content-Disposition":f'inline; filename="{path.name}"'})

@app.get("/pdf/download/{name}")
async def download_pdf(name: str):
    path=safe_storage_path(name)
    return FileResponse(path,media_type="application/pdf",filename=path.name)

@app.get("/sellos", response_class=HTMLResponse)
async def sellos_page(request: Request):
    return HTMLResponse('<a href="/">Volver</a><h3>Sellos OK</h3>')

@app.get("/calibrar", response_class=HTMLResponse)
async def calibrar_page(request: Request):
    return HTMLResponse('<a href="/">Volver</a><h3>Calibrar OK - QR [5,2,60,57] MOLDABLE [65,8,200,45]</h3>')
