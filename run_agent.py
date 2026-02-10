import os
import sys
import json
import time
import subprocess
import yaml
import re
from datetime import datetime, timezone

# --- CONFIGURATION ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Using a list of models to handle availability issues (404/NotFoundError)
MODELS = ["claude-3-5-sonnet-20240620", "claude-3-5-sonnet-latest", "claude-3-7-sonnet-20250219"]
TASK_FILE = "task.yaml"

def get_timestamp():
    return datetime.now(timezone.utc).isoformat().split('.')[0] + "Z"

def log_jsonl(entry):
    """Write agent action to agent.log in strict JSONL format."""
    with open("agent.log", "a") as f:
        f.write(json.dumps(entry) + "\n")

# --- TOOLS ---
def run_bash(command, cwd="/testbed"):
    log_jsonl({"timestamp": get_timestamp(), "type": "tool_use", "tool": "run_bash", "args": {"command": command}})
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=cwd)
        output = result.stdout + result.stderr
        # Check for missing modules and fix them on the fly
        if "ModuleNotFoundError: No module named" in output:
            module_match = re.search(r"ModuleNotFoundError: No module named '([^']+)'", output)
            if module_match:
                module_name = module_match.group(1)
                pkg = "python-memcached" if module_name == "memcache" else module_name
                print(f"Autonomous Fix: Missing module {module_name} detected. Installing {pkg}...")
                subprocess.run(f"pip install {pkg}", shell=True)
                # Retry original command
                result = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=cwd)
                output = result.stdout + result.stderr
        return output, result.returncode
    except Exception as e:
        return str(e), -1

def read_file(path, cwd="/testbed"):
    log_jsonl({"timestamp": get_timestamp(), "type": "tool_use", "tool": "read_file", "args": {"path": path}})
    full_path = os.path.join(cwd, path) if not path.startswith("/") else path
    try:
        with open(full_path, "r") as f:
            return f.read(), None
    except Exception as e:
        return None, str(e)

def edit_file(path, old_str, new_str, cwd="/testbed"):
    log_jsonl({"timestamp": get_timestamp(), "type": "tool_use", "tool": "edit_file", "args": {"path": path, "old_str": old_str, "new_str": new_str}})
    full_path = os.path.join(cwd, path) if not path.startswith("/") else path
    try:
        with open(full_path, "r") as f: content = f.read()
        if old_str not in content:
            return None, f"Error: '{old_str}' not found in {path}"
        new_content = content.replace(old_str, new_str)
        with open(full_path, "w") as f: f.write(new_content)
        return "Success: File edited.", None
    except Exception as e:
        return None, str(e)

# --- AGENT CORE ---
def main():
    if not ANTHROPIC_API_KEY:
        print("Missing ANTHROPIC_API_KEY")
        sys.exit(1)
    
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Initialize Log
    if os.path.exists("agent.log"): os.remove("agent.log")
    
    with open(TASK_FILE, "r") as f: task = yaml.safe_load(f)
    test_cmd = task['tests']['test_command']

    # 1. PRE-VERIFICATION
    print("Pre-verification (Bug Reproduction)...")
    pre_out, _ = run_bash(test_cmd)
    with open("pre_verification.log", "w") as f: f.write(pre_out)

    # 2. AI LOOP
    system_prompt = f"""You are an expert SWE agent. Fix the bug in OpenLibrary.
Task: {task['description']}
Requirements:
1. Implement 'find_staged_or_pending' as a @classmethod in 'ImportItem' class.
2. Path: openlibrary/core/imports.py
3. Use STAGED_SOURCES = ('amazon', 'idb') as global or class constant.
4. Use 'db.get_db().select' for queries.
TOOLS: read_file, edit_file, run_bash.
"""
    
    messages = [{"role": "user", "content": f"The tests are failing with:\n{pre_out}\nPlease fix the logic."}]
    history_md = []

    for i in range(10):
        log_jsonl({"timestamp": get_timestamp(), "type": "request", "content": messages[-1]['content']})
        
        response = None
        for model_name in MODELS:
            try:
                response = client.messages.create(
                    model=model_name,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=messages,
                    tools=[
                        {"name": "run_bash", "description": "Execute bash commands", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
                        {"name": "read_file", "description": "Read a file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
                        {"name": "edit_file", "description": "Replace string in file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}}, "required": ["path", "old_str", "new_str"]}}
                    ]
                )
                break
            except Exception as e:
                if "not_found" in str(e).lower(): continue
                print(f"Model Error: {e}")
                break
        
        if not response: break
        
        log_jsonl({"timestamp": get_timestamp(), "type": "response", "content": str(response.content)})
        
        assistant_text = ""
        tool_calls = []
        for content in response.content:
            if content.type == 'text': assistant_text += content.text
            if content.type == 'tool_use': tool_calls.append(content)
        
        messages.append({"role": "assistant", "content": response.content})
        history_md.append(f"### Turn {i+1}\n\n**Agent:** {assistant_text}")

        if not tool_calls: break

        tool_results = []
        for tc in tool_calls:
            print(f"Action: {tc.name}")
            if tc.name == "run_bash": res, _ = run_bash(tc.input['command'])
            elif tc.name == "read_file": res, _ = read_file(tc.input['path'])
            elif tc.name == "edit_file": res, _ = edit_file(tc.input['path'], tc.input['old_str'], tc.input['new_str'])
            
            tool_results.append({"type": "tool_result", "tool_use_id": tc.id, "content": str(res)})
            history_md.append(f"**Tool {tc.name}:** {res}")
        
        messages.append({"role": "user", "content": tool_results})

    with open("prompts.md", "w") as f:
        f.write("# Engineering History\n\n" + "\n\n".join(history_md))

    # 3. POST-VERIFICATION
    print("Post-verification (Success Check)...")
    post_out, _ = run_bash(test_cmd)
    with open("post_verification.log", "w") as f: f.write(post_out)
    
    # 4. EXPORT PATCH
    diff, _ = run_bash("git diff", cwd="/testbed")
    with open("changes.patch", "w") as f: f.write(diff)
    print("Automation Complete.")

if __name__ == "__main__": main()
