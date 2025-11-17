#!/usr/bin/env python3
"""Check which dependencies from requirements.txt are missing"""

import subprocess
import sys

# Read requirements.txt
with open('requirements.txt', 'r') as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith('#')]

# Get installed packages
result = subprocess.run([sys.executable, '-m', 'pip', 'list', '--format=freeze'], 
                       capture_output=True, text=True)
installed = {}
for line in result.stdout.split('\n'):
    if '==' in line:
        pkg, version = line.split('==', 1)
        installed[pkg.lower()] = version

print("Checking dependencies from requirements.txt...\n")
missing = []
installed_packages = []

for req in requirements:
    if '>=' in req:
        pkg_name = req.split('>=')[0].strip().lower()
        min_version = req.split('>=')[1].strip()
    elif '==' in req:
        pkg_name = req.split('==')[0].strip().lower()
        min_version = req.split('==')[1].strip()
    else:
        pkg_name = req.strip().lower()
        min_version = None
    
    if pkg_name in installed:
        installed_packages.append(f"[OK] {req.split('>=')[0].split('==')[0].strip():30} {installed[pkg_name]}")
    else:
        missing.append(req)

print("INSTALLED:")
for pkg in installed_packages:
    print(f"  {pkg}")

print("\nMISSING:")
if missing:
    for pkg in missing:
        print(f"  [MISSING] {pkg}")
else:
    print("  None!")

print(f"\nTotal: {len(installed_packages)} installed, {len(missing)} missing")

