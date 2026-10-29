"""Merge live Copilot limits with the requesting Codex binary's own model defaults."""
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def ensure_defaults(root, command='codex'):
    executable = shutil.which(command)
    if not executable:
        raise RuntimeError('Codex executable not found. Use --codex with the desktop bundled codex.exe.')
    stat = Path(executable).stat()
    identity = [str(Path(executable).resolve()), stat.st_size, stat.st_mtime_ns]
    path = root/'catalog-defaults.json'
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('executable_identity') == identity:
            return saved
    except (OSError, ValueError):
        pass
    version_result = subprocess.run([executable,'--version'],capture_output=True,text=True,encoding='utf-8',timeout=10,check=True)
    match = re.search(r'\b(\d+\.\d+\.\d+)',version_result.stdout)
    if not match:
        raise RuntimeError('Could not identify the installed Codex schema version.')
    result = subprocess.run([executable,'debug','models','--bundled'],capture_output=True,text=True,encoding='utf-8',timeout=30,check=True)
    defaults = json.loads(result.stdout)
    if not isinstance(defaults.get('models'),list) or not defaults['models']:
        raise RuntimeError('Codex did not return its bundled model catalog.')
    saved = {'client_version':match.group(1),'executable_identity':identity,'catalog':defaults}
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=root,delete=False) as f:
        json.dump(saved,f,ensure_ascii=False,separators=(',',':'))
        tmp=Path(f.name)
    try:
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)
    return saved


def positive(value):
    return isinstance(value,int) and not isinstance(value,bool) and value>0


def adapt(upstream, defaults):
    """Only exact known model IDs; never borrow another model's instructions."""
    available={m.get('id'):m for m in upstream.get('data',[]) if isinstance(m,dict)}
    result=copy.deepcopy(defaults)
    result['models']=[]
    for base in defaults['models']:
        remote=available.get(base['slug'])
        if not remote or remote.get('policy',{}).get('state')!='enabled':
            continue
        if '/responses' not in remote.get('supported_endpoints',[]):
            continue
        caps=remote.get('capabilities',{})
        support=caps.get('supports',{})
        if support.get('tool_calls') is not True or support.get('streaming') is not True:
            continue
        limits=caps.get('limits',{})
        total=limits.get('max_context_window_tokens')
        output=limits.get('max_output_tokens')
        prompt=limits.get('max_prompt_tokens')
        if not all(positive(v) for v in (total,output,prompt)) or output>=total:
            continue
        # Codex's usable input context must leave room for generated tokens.
        budget=min(prompt,total-output)
        model=copy.deepcopy(base)
        model['context_window']=budget
        model['max_context_window']=budget
        # Remove a fixed compaction threshold, if a future bundled model carries one.
        # The runtime can derive its own threshold from the updated context size.
        model.pop('auto_compact_token_limit',None)
        if isinstance(support.get('reasoning_effort'),list):
            model['supported_reasoning_levels']=[r for r in base.get('supported_reasoning_levels',[]) if r['effort'] in support['reasoning_effort']]
            choices=[r['effort'] for r in model['supported_reasoning_levels']]
            if not choices:
                continue
            if model.get('default_reasoning_level') not in choices:
                model['default_reasoning_level']=choices[0]
        if support.get('vision') is not True:
            model['input_modalities']=[m for m in model.get('input_modalities',[]) if m!='image']
        # Copilot does not advertise OpenAI service tiers or migration routes.
        model['service_tiers']=[]
        model['additional_speed_tiers']=[]
        model['default_service_tier']=None
        model['upgrade']=None
        model['supported_in_api']=True
        result['models'].append(model)
    if not result['models']:
        raise RuntimeError('No enabled Responses models match this Codex version and have complete limits.')
    return result
