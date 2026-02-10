import os
import sys
import json
import time
import subprocess
import yaml
import re
from anthropic import Anthropic

# Configuration
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TASK_FILE = "task.yaml"
# Using stable Sonnet model
MODELS = ["claude-3-5-sonnet-20240620", "claude-3-5-sonnet-latest", "claude-3-sonnet-20240229"]

def log_jsonl(entry):
    with open("agent.log", "a") as f:
        f.write(json.dumps(entry) + "\n")

def get_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def run_bash(command, cwd="/testbed"):
    log_jsonl({"timestamp": get_timestamp(), "type": "tool_use", "tool": "run_bash", "args": {"command": command}})
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=cwd)
        return result.stdout + result.stderr, result.returncode
    except Exception as e:
        return str(e), -1

def read_file(path, cwd="/testbed"):
    log_jsonl({"timestamp": get_timestamp(), "type": "tool_use", "tool": "read_file", "args": {"path": path}})
    full_path = os.path.join(cwd, path) if not path.startswith("/") else path
    try:
        if not os.path.exists(full_path): return None, f"File {path} not found"
        with open(full_path, "r") as f: return f.read(), None
    except Exception as e: return None, str(e)

def write_file(path, content, cwd="/testbed"):
    log_jsonl({"timestamp": get_timestamp(), "type": "tool_use", "tool": "write_file", "args": {"path": path, "content": "[Content hidden]"}})
    full_path = os.path.join(cwd, path) if not path.startswith("/") else path
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f: f.write(content)
        return "success", None
    except Exception as e: return None, str(e)

def edit_file(path, old_str, new_str, cwd="/testbed"):
    log_jsonl({"timestamp": get_timestamp(), "type": "tool_use", "tool": "edit_file", "args": {"path": path, "old_str": old_str, "new_str": new_str}})
    full_path = os.path.join(cwd, path) if not path.startswith("/") else path
    try:
        with open(full_path, "r") as f: content = f.read()
        if old_str not in content: return None, f"String '{old_str}' not found in {path}"
        new_content = content.replace(old_str, new_str)
        with open(full_path, "w") as f: f.write(new_content)
        return "success", None
    except Exception as e: return None, str(e)

def call_anthropic(client, messages, system_prompt):
    tools = [
        {"name": "run_bash", "description": "Run bash command", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "Read file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "write_file", "description": "Overwrite/Create file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
        {"name": "edit_file", "description": "Replace string in file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}}, "required": ["path", "old_str", "new_str"]}}
    ]
    
    log_jsonl({"timestamp": get_timestamp(), "type": "request", "content": str(messages[-1]['content'])})
    
    error_msg = ""
    for model_name in MODELS:
        try:
            print(f"Calling Anthropic [{model_name}]...")
            response = client.messages.create(
                model=model_name,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=tools
            )
            log_jsonl({"timestamp": get_timestamp(), "type": "response", "content": str(response.content)})
            # Also log to a human-readable prompts.log for the summary script if needed
            with open("prompts.log", "a") as f: f.write(json.dumps({"model": model_name, "usage": response.usage.to_dict() if hasattr(response, 'usage') else {}}) + "\n")
            return response
        except Exception as e:
            error_msg = str(e)
            print(f"Model {model_name} failed: {error_msg}")
            if "not_found" in error_msg.lower(): continue
            break
    raise Exception(f"All models failed. Last error: {error_msg}")

def main():
    if not API_KEY: sys.exit(1)
    client = Anthropic(api_key=API_KEY)
    if os.path.exists("agent.log"): os.remove("agent.log")
    if os.path.exists("prompts.log"): os.remove("prompts.log")
    
    with open(TASK_FILE, "r") as f: task = yaml.safe_load(f)
    test_cmd = task['tests']['test_command']

    print("Step 1: Pre-verification (Expect Failure)")
    pre_out, _ = run_bash(test_cmd)
    with open("pre_verification.log", "w") as f: f.write(pre_out)

    system_prompt = f"""You are an autonomous SWE. Fix the bug in OpenLibrary.
Task: {task['description']}
Requirement: Implement find_staged_or_pending in openlibrary/core/imports.py.
Use STAGED_SOURCES = ('amazon', 'idb').
Check local records before API calls.
You MUST use 'edit_file' or 'write_file' to apply changes.
Reproduce failure with: {test_cmd}
"""

    messages = [{"role": "user", "content": f"Tests are failing:\n{pre_out}\nPlease fix the logic."}]
    
    for i in range(10):
        try:
            res = call_anthropic(client, messages, system_prompt)
            messages.append({"role": "assistant", "content": res.content})
            
            tool_calls = [c for c in res.content if c.type == 'tool_use']
            if not tool_calls: break
            
            tool_res = []
            for tc in tool_calls:
                n, a, tid = tc.name, tc.input, tc.id
                print(f"Action: {n}")
                if n == "run_bash": 
                    val, err = run_bash(a['command'])
                    # Autonomous Dependency Fix: If we see ModuleNotFoundError, try to install it
                    module_match = re.search(r"ModuleNotFoundError: No module named '([^']+)'", val)
                    if module_match:
                        missing_module = module_match.group(1)
                        print(f"Detected missing module: {missing_module}. Attempting install...")
                        run_bash(f"pip install {missing_module}")
                        # Retry the command once
                        val, err = run_bash(a['command'])
                elif n == "read_file": val, err = read_file(a['path'])
                elif n == "write_file": val, err = write_file(a['path'], a['content'])
                elif n == "edit_file": val, err = edit_file(a['path'], a['old_str'], a['new_str'])
                tool_res.append({"type": "tool_result", "tool_use_id": tid, "content": str(val or err)})
            messages.append({"role": "user", "content": tool_res})
        except Exception as e:
            print(f"Fatal error: {e}")
            break

    print("Step 2: Post-verification (Expect Success)")
    post_out, _ = run_bash(test_cmd)
    with open("post_verification.log", "w") as f: f.write(post_out)
    
    diff, _ = run_bash("git diff", cwd="/testbed")
    with open("changes.patch", "w") as f: f.write(diff)
    
    with open("prompts.md", "w") as f:
        f.write("# Engineering Sessions\n\n")
        for m in messages:
            f.write(f"## {m['role'].upper()}\n\n{str(m['content'])}\n\n")

if __name__ == "__main__": main()
