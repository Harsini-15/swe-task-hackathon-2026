import os
import sys
import json
import time
import subprocess
import requests
import yaml

# Configuration
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TASK_FILE = "task.yaml"

def log_to_agent_log(timestamp, msg_type, content=None, tool=None, args=None):
    """Log in specific JSONL format for agent.log."""
    entry = {"timestamp": timestamp, "type": msg_type}
    if content: entry["content"] = content
    if tool: entry["tool"] = tool
    if args: entry["args"] = args
    with open("agent.log", "a") as f:
        f.write(json.dumps(entry) + "\n")

def run_bash(command, cwd="/testbed"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log_to_agent_log(ts, "tool_use", tool="run_bash", args={"command": command})
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=cwd)
        output = result.stdout + result.stderr
        return output, result.returncode
    except Exception as e:
        return str(e), -1

def read_file(path, cwd="/testbed"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log_to_agent_log(ts, "tool_use", tool="read_file", args={"path": path})
    full_path = os.path.join(cwd, path) if not path.startswith("/") else path
    try:
        with open(full_path, "r") as f: return f.read(), None
    except Exception as e: return None, str(e)

def write_file(path, content, cwd="/testbed"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log_to_agent_log(ts, "tool_use", tool="write_file", args={"path": path, "content": "[hidden]"})
    full_path = os.path.join(cwd, path) if not path.startswith("/") else path
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f: f.write(content)
        return "success", None
    except Exception as e: return None, str(e)

def edit_file(path, old_str, new_str, cwd="/testbed"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log_to_agent_log(ts, "tool_use", tool="edit_file", args={"path": path, "old_str": old_str, "new_str": new_str})
    full_path = os.path.join(cwd, path) if not path.startswith("/") else path
    try:
        with open(full_path, "r") as f: content = f.read()
        if old_str not in content: return None, "String not found"
        new_content = content.replace(old_str, new_str)
        with open(full_path, "w") as f: f.write(new_content)
        return "success", None
    except Exception as e: return None, str(e)

def call_agent(messages, system_prompt):
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": API_KEY.strip(), "anthropic-version": "2023-06-01", "content-type": "application/json"}
    tools = [
        {"name": "run_bash", "description": "Run bash", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "Read file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "write_file", "description": "Write file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
        {"name": "edit_file", "description": "Edit file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}}, "required": ["path", "old_str", "new_str"]}}
    ]

    log_to_agent_log(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "request", content=str(messages[-1]))

    data = {"model": "claude-3-5-sonnet-20241022", "max_tokens": 4096, "system": system_prompt, "messages": messages, "tools": tools}
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    result = response.json()
    
    log_to_agent_log(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "response", content=str(result['content']))
    return result

def main():
    if os.path.exists("agent.log"): os.remove("agent.log")
    with open(TASK_FILE, "r") as f: task = yaml.safe_load(f)
    
    # Pre-verification
    out, code = run_bash(task['tests']['test_command'])
    with open("pre_verification.log", "w") as f: f.write(out)

    system_prompt = f"Solve the task in /testbed. ID: {task['task_id']}\n{task['description']}\nRequirements: {task['requirements']}\nInterface: {task['interface']}"
    messages = [{"role": "user", "content": f"Tests failed:\n{out}\nPlease fix."}]

    for _ in range(10):
        try:
            res = call_agent(messages, system_prompt)
            messages.append({"role": "assistant", "content": res['content']})
            tool_calls = [c for c in res['content'] if c['type'] == 'tool_use']
            if not tool_calls: break
            
            tool_outputs = []
            for tc in tool_calls:
                n, a, tid = tc['name'], tc['input'], tc['id']
                if n == "run_bash": val, err = run_bash(a['command'])
                elif n == "read_file": val, err = read_file(a['path'])
                elif n == "write_file": val, err = write_file(a['path'], a['content'])
                elif n == "edit_file": val, err = edit_file(a['path'], a['old_str'], a['new_str'])
                tool_outputs.append({"type": "tool_result", "tool_use_id": tid, "content": str(val or err)})
            messages.append({"role": "user", "content": tool_outputs})
        except Exception: break

    # Post-verification
    out, code = run_bash(task['tests']['test_command'])
    with open("post_verification.log", "w") as f: f.write(out)
    
    diff, _ = run_bash("git diff", cwd="/testbed")
    with open("changes.patch", "w") as f: f.write(diff)

    with open("prompts.md", "w") as f:
        f.write("# Prompts and History\n\n")
        for m in messages: f.write(f"## {m['role']}\n\n{json.dumps(m['content'], indent=2)}\n\n")

if __name__ == "__main__": main()
