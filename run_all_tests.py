import subprocess
print("Running full test suite...")
result = subprocess.run(["python3", "-m", "pytest", "-q"], capture_output=True, text=True)
print("Return code:", result.returncode)
print(result.stdout)
if result.stderr:
    print("Errors:")
    print(result.stderr)
