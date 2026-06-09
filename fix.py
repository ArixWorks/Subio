import os

def replace_in_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    new_content = content.replace('subio-ssh', 'hpnssh')
    new_content = new_content.replace('subio-sshd', 'hpnsshd')
    new_content = new_content.replace('SUBIO-SSH', 'HPN-SSH')
    new_content = new_content.replace('ppa:rapier1/subio', 'ppa:rapier1/hpn')
    
    if content != new_content:
        with open(filepath, 'w', encoding='utf-8', newline='\n') as f:
            f.write(new_content)
        print(f"Updated {filepath}")

for root, _, files in os.walk(r'f:\Meli\mori\11\hpn\hpn-tun'):
    for file in files:
        if file.endswith(('.sh', '.py', '.service', '.template', '.md')):
            replace_in_file(os.path.join(root, file))
