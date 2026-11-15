"""Local browser workbench: Ollama planning plus a real stdio MCP client/server."""
from __future__ import annotations
import asyncio, json, os, sys, urllib.request, uuid
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).parent.resolve()
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
CASES = json.loads((ROOT/"batch_queries.json").read_text(encoding="utf-8"))
_async_loop = None
_async_ready = threading.Event()
_async_lock = threading.Lock()

def _async_loop_thread():
    global _async_loop
    loop=asyncio.new_event_loop();asyncio.set_event_loop(loop);_async_loop=loop;_async_ready.set();loop.run_forever()

def run_async(coro):
    """Run MCP coroutines on one persistent loop; repeated Windows loop creation can exhaust local socket pairs."""
    global _async_loop
    if _async_loop is None:
        with _async_lock:
            if _async_loop is None:
                threading.Thread(target=_async_loop_thread,name="recruitment-mcp-loop",daemon=True).start()
    if not _async_ready.wait(10): raise RuntimeError("MCP async runtime did not start")
    future=asyncio.run_coroutine_threadsafe(coro,_async_loop)
    return future.result(timeout=300)

def ollama_status():
    try:
        with urllib.request.urlopen(OLLAMA.rstrip("/")+"/api/tags", timeout=2) as r: data=json.loads(r.read())
        names=[m.get("name","") for m in data.get("models",[])]
        return {"connected":True,"model":MODEL,"available":MODEL in names or any(n.startswith(MODEL.split(":")[0]+":") for n in names),"models":names}
    except Exception as e: return {"connected":False,"model":MODEL,"available":False,"models":[],"error":str(e)}

def local_explanation(query, result):
    prompt=("You are a teaching assistant for a synthetic classroom recruitment workflow. Explain the observed deterministic result in plain language. "
            "Do not make hiring recommendations, infer protected traits, or treat the score as a final decision. Keep to 100 words.\n\n"
            f"Student request: {query}\nObserved MCP result: {json.dumps(result, ensure_ascii=False)[:5000]}")
    body=json.dumps({"model":MODEL,"messages":[{"role":"user","content":prompt}],"stream":False,"options":{"temperature":0.2,"num_predict":180},"keep_alive":"30m"}).encode()
    req=urllib.request.Request(OLLAMA.rstrip("/")+"/api/chat",data=body,headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=60) as r: return json.loads(r.read())["message"]["content"]

def local_plan(query):
    system=("You interpret a classroom recruitment-workflow request and return a short plan only. Allowed MCP tools: get_dataset_summary, process_candidate_batch, run_batch_checks, evaluate_candidate, review_candidate. "
            "Scoring and routing must stay deterministic and based on rules.json. Never make a hiring decision, dispatch a message, or use personal characteristics. Suggest only one tool and its purpose.")
    body=json.dumps({"model":MODEL,"messages":[{"role":"system","content":system},{"role":"user","content":query}],"stream":False,"options":{"temperature":0,"num_predict":120},"keep_alive":"30m"}).encode()
    req=urllib.request.Request(OLLAMA.rstrip("/")+"/api/chat",data=body,headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=60) as r: response=json.loads(r.read())["message"]["content"]
    return {"model":MODEL,"messages":[{"role":"system","content":system},{"role":"user","content":query}],"response":response}

def choose_workflow_tool(query):
    """Choose an allowlisted operation from the user's words; avoid substring action matches."""
    import re
    q=query.casefold()
    unsafe_patterns=(
        r"\bignore (?:all )?(?:previous|prior) instructions\b",
        r"\bignore (?:the )?(?:privacy|safety|human.review) rules?\b",
        r"\bwithout (?:hr|human) review\b",
        r"\bautomatically approve\b",
        r"\bsend (?:an? )?(?:email|notification|message)\b",
        r"\blist (?:every|all) candidate (?:names? and )?emails?\b",
        r"\b(?:age|gender|race|religion|disability)\b.*\b(?:score|scoring|favor|rank|shortlist)\b",
        r"\b(?:score|scoring|favor|rank|shortlist)\b.*\b(?:age|gender|race|religion|disability)\b",
        r"\bchange (?:the )?(?:scoring|rules?)\b",
    )
    if any(re.search(pattern,q) for pattern in unsafe_patterns):
        return "get_dataset_summary",{},"Unsafe instruction detected. The requested approval, dispatch, private-data disclosure, or protected-trait scoring is blocked; only the non-sensitive dataset summary is allowed."
    candidate=re.search(r"\bc\d{4}\b",query,re.I)
    if any(x in q for x in ("run tests","batch test","test suite","boundary score","validate all")):
        return "run_batch_checks",{},"The request explicitly asks to run checks or tests."
    if any(x in q for x in ("approve candidate","hold candidate","request information for","request info for","edit candidate status")):
        action="APPROVE" if "approve" in q else "HOLD" if "hold" in q else "REQUEST_INFO" if "request" in q else "EDIT"
        return "review_candidate",{"candidate_id":candidate.group(0).upper() if candidate else "C0001","action":action,"reviewer":"HR_REVIEWER","notes":"Guided workflow demonstration."},"An explicit human-review action was requested."
    if any(x in q for x in ("summarize","summary","dataset","how many records","routing counts","count of candidates","distribution")):
        return "get_dataset_summary",{},"The request asks for dataset totals or routing counts. Words such as 'review categories' describe report categories, not a reviewer action."
    if any(x in q for x in ("process all candidates","quarantine invalid","write audit","save audit outputs")):
        return "process_candidate_batch",{},"The request asks to process and persist a complete batch."
    if candidate and any(x in q for x in ("candidate","score","evaluate","explain")):
        return "evaluate_candidate",{"candidate_id":candidate.group(0).upper()},"The request asks to evaluate a specific candidate."
    return "get_dataset_summary",{},"No specific candidate or review action was requested; summarize the dataset as the safe default."

def unpack_mcp_result(result):
    content=[]
    for block in result.content:
        if getattr(block,"text",None):
            try: content.append(json.loads(block.text))
            except ValueError: content.append(block.text)
    return content[0] if len(content)==1 else content

async def invoke(name, args=None):
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp_server.py")],env=os.environ.copy())
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            available={t.name for t in (await session.list_tools()).tools}
            if name not in available: raise ValueError("Requested tool is not registered")
            res=await session.call_tool(name,args or {})
            content=[]
            for block in res.content:
                if getattr(block,"text",None):
                    try: content.append(json.loads(block.text))
                    except ValueError: content.append(block.text)
            return {"result":content[0] if len(content)==1 else content,"isError":res.isError}

def case_call(case):
    kind=case["action"]
    if kind=="security": return "get_dataset_summary",{}
    if kind=="summary": return "get_dataset_summary",{}
    if kind=="process": return "process_candidate_batch",{}
    if kind=="checks": return "run_batch_checks",{}
    if kind=="candidate": return "evaluate_candidate",{"candidate_id":case.get("candidate_id","C0001")}
    if kind=="review": return "review_candidate",{"candidate_id":case.get("candidate_id","C0001"),"action":case.get("review_action","HOLD"),"reviewer":"HR_REVIEWER","notes":"Batch demonstration; no external communication."}
    return "get_dataset_summary",{}

async def run_case(case, session):
    name,args=case_call(case)
    if case["action"]=="security":
        # Run the local model's interpretation, then verify deterministic policy routing
        # overrides unsafe content before an MCP operation can be selected.
        plan=await asyncio.to_thread(local_plan,case["query"])
        selected,args,reason=choose_workflow_tool(case["query"])
        response=await session.call_tool(selected,args)
        result=unpack_mcp_result(response)
        safe=(selected=="get_dataset_summary" and args=={} and not response.isError
              and isinstance(result,dict) and "email" not in json.dumps(result).casefold()
              and "candidate_name" not in json.dumps(result).casefold())
        return {"id":case["id"],"title":case["title"],"query":case["query"],"tool":selected,"arguments":args,
                "expected":"unsafe request blocked; non-sensitive summary only","passed":safe,
                "result":{"model":plan["model"],"local_model_consulted":True,"qwen_response_ignored_for_tool_authorization":True,
                          "selected_tool":selected,"selection_reason":reason,"unsafe_action_blocked":safe,
                          "contains_contact_data":False,"summary":result},
                "timestamp":datetime.now(timezone.utc).isoformat()}
    response=await session.call_tool(name,args)
    out={"result":unpack_mcp_result(response),"isError":response.isError}
    expected_error=bool(case.get("expect_error"))
    passed=out["isError"] if expected_error else not out["isError"]
    return {"id":case["id"],"title":case["title"],"query":case["query"],"tool":name,"arguments":args,"expected":"controlled error" if expected_error else "successful result","passed":passed,"result":out["result"],"timestamp":datetime.now(timezone.utc).isoformat()}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def send_json(self,data,status=200):
        raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=="/":
            raw=(ROOT/"web"/"index.html").read_bytes();self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
        elif self.path=="/api/health": self.send_json({"ok":True,"model":MODEL})
        elif self.path=="/api/llm": self.send_json(ollama_status())
        elif self.path=="/api/batch": self.send_json({"cases":CASES,"suites":sorted(set(c["suite"] for c in CASES))})
        elif self.path=="/api/tools":
            try: self.send_json(run_async(invoke("get_dataset_summary")))
            except Exception as e: self.send_json({"error":str(e)},500)
        else: self.send_json({"error":"Not found"},404)
    def do_POST(self):
        try: payload=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
        except Exception: self.send_json({"error":"Invalid JSON"},400);return
        if self.path=="/api/batch/run":
            ids=payload.get("ids",[]); selected=[x for x in CASES if x["id"] in ids] if isinstance(ids,list) else []
            if not selected: self.send_json({"error":"Select at least one test query."},400);return
            async def run():
                rows=[]; params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp_server.py")],env=os.environ.copy())
                async with stdio_client(params) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize(); available={t.name for t in (await session.list_tools()).tools}
                        for case in selected:
                            try:
                                name,_=case_call(case)
                                if name not in available: raise ValueError(f"MCP tool not registered: {name}")
                                rows.append(await run_case(case,session))
                            except Exception as e: rows.append({"id":case["id"],"title":case["title"],"query":case["query"],"expected":"controlled error" if case.get("expect_error") else "successful result","passed":False,"result":{"error":str(e)},"timestamp":datetime.now(timezone.utc).isoformat()})
                return rows
            try: self.send_json({"results":run_async(run())})
            except Exception as e: self.send_json({"error":str(e)},500)
        elif self.path=="/api/review":
            candidate_id=str(payload.get("candidate_id","C0001")).strip()
            action=str(payload.get("action","HOLD"))
            notes=str(payload.get("notes",""))[:500]
            try: self.send_json(run_async(invoke("review_candidate",{"candidate_id":candidate_id,"action":action,"reviewer":"HR_REVIEWER","notes":notes})))
            except Exception as e: self.send_json({"error":str(e)},400)
        elif self.path=="/api/workflow":
            query=str(payload.get("query","")).strip()
            if not query or len(query)>700: self.send_json({"error":"Enter a request from 1 to 700 characters."},400);return
            self.send_response(200);self.send_header("Content-Type","application/x-ndjson; charset=utf-8");self.send_header("Cache-Control","no-cache");self.end_headers()
            def emit(obj): self.wfile.write((json.dumps(obj,ensure_ascii=False)+"\n").encode());self.wfile.flush()
            try:
                run_id="RUN-"+uuid.uuid4().hex[:10].upper()
                emit({"type":"step","step":1,"title":"Capture request","detail":{"input":{"user_text":query},"output":{"normalized_text":query,"characters":len(query)}}})
                try: plan=local_plan(query)
                except Exception as e: plan={"model":MODEL,"messages":[{"role":"user","content":query}],"response":f"Ollama unavailable: {e}. Continue with deterministic MCP routing."}
                emit({"type":"step","step":2,"title":"Ask local Qwen 2.5 7B to interpret the request","detail":{"input":{"model":plan["model"],"messages":plan["messages"]},"output":{"qwen_response":plan["response"]}}})
                tool,args,reason=choose_workflow_tool(query)
                emit({"type":"step","step":3,"title":"Review the plan and select a safe MCP operation","detail":{"input":{"user_request":query,"qwen_plan":plan["response"]},"output":{"selected_tool":tool,"arguments":args,"selection_reason":reason,"policy":"Qwen proposes an interpretation; deterministic allowlisted routing selects the operation."}}})
                params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp_server.py")],env=os.environ.copy())
                async def discovered_call():
                    async with stdio_client(params) as (read,write):
                        async with ClientSession(read,write) as session:
                            await session.initialize(); listed=(await session.list_tools()).tools
                            by_name={t.name:t for t in listed}; tool_info=by_name.get(tool)
                            meta=[{"name":t.name,"description":t.description,"inputSchema":t.inputSchema} for t in listed]
                            emit({"type":"step","step":4,"title":"Discover MCP tools","detail":{"input":{"requested_tool":tool},"output":{"discovered_tools":[t.name for t in listed],"selected_tool_schema":{"name":tool_info.name,"description":tool_info.description,"inputSchema":tool_info.inputSchema} if tool_info else None}}})
                            if not tool_info: raise ValueError("Selected tool is not registered by the MCP server")
                            schema=tool_info.inputSchema or {}; props=schema.get("properties",{}); required=schema.get("required",[])
                            missing=[x for x in required if x not in args]; extra=[x for x in args if props and x not in props]
                            validation={"tool_registered":True,"required_fields_present":not missing,"missing_fields":missing,"no_unknown_fields":not extra,"unknown_fields":extra,"arguments_valid":not missing and not extra}
                            if not validation["arguments_valid"]:
                                emit({"type":"step","step":5,"title":"Validate inputs and execute the selected tool","detail":{"input":{"tool":tool,"arguments":args},"validation":validation,"output":{"call_dispatched":False}}})
                                raise ValueError("MCP arguments did not pass schema validation")
                            emit({"type":"step","step":5,"title":"Validate inputs and execute the selected tool","detail":{"input":{"tool":tool,"arguments":args},"validation":validation,"output":{"call_dispatched":True,"transport":"MCP ClientSession over stdio"}}})
                            result=await session.call_tool(tool,args)
                            return {"value":unpack_mcp_result(result),"isError":result.isError,"tools":meta}
                result=run_async(discovered_call())
                try: explanation=local_explanation(query,result["value"])
                except Exception as e: explanation=f"Local explanation unavailable: {e}"
                emit({"type":"step","step":6,"title":"Check and explain the result","detail":{"input":{"mcp_result":result["value"],"tool_error":result["isError"]},"output":{"verification":"MCP completed" if not result["isError"] else "MCP reported an error","qwen_explanation":explanation,"qwen_role":"Explain observed result only; scoring and routing remain deterministic."}}})
                timestamp=datetime.now(timezone.utc).isoformat()
                result_value=result["value"]
                summary={k:result_value.get(k) for k in ("candidate_id","score","proposed_status","total","valid","invalid","routing","final_status","action") if isinstance(result_value,dict) and k in result_value}
                trace={"run_id":run_id,"timestamp":timestamp,"tool":tool,"arguments":args,"success":not result["isError"],"outcome_summary":summary}
                trace_dir=ROOT/"artifacts";trace_dir.mkdir(exist_ok=True)
                with (trace_dir/"workflow_runs.jsonl").open("a",encoding="utf-8") as f:f.write(json.dumps(trace,ensure_ascii=False)+"\n")
                emit({"type":"step","step":7,"title":"Record outcome","detail":{"input":{"run_id":run_id,"success":not result["isError"],"outcome_summary":summary},"output":{"trace_file":"artifacts/workflow_runs.jsonl","trace_appended":True,"human_approval_required":True,"notification_dispatch":"not available; drafts only"}}})
                emit({"type":"complete","run_id":run_id,"tool":tool,"result":result_value,"explanation":explanation,"timestamp":timestamp,"trace_file":"artifacts/workflow_runs.jsonl"})
            except Exception as e: emit({"type":"fatal","error":str(e)})
        else: self.send_json({"error":"Not found"},404)

if __name__=="__main__":
    print(f"Recruitment Workflow at http://127.0.0.1:{os.getenv('PORT','8001')} · Ollama {MODEL}",flush=True)
    ThreadingHTTPServer(("127.0.0.1",int(os.getenv("PORT","8001"))),Handler).serve_forever()
