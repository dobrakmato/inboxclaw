import subprocess
import sys
import os

def run_e2e():
    print("🚀 Running all E2E tests...")
    
    # Ensure PYTHONPATH is set to include the project root
    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()
    
    # Build pytest command
    cmd = [sys.executable, "-m", "pytest", "-v", "e2e"]
    
    try:
        # Run pytest
        result = subprocess.run(cmd, env=env)
        
        if result.returncode == 0:
            print("\n✅ All E2E tests passed successfully!")
        else:
            print(f"\n❌ E2E tests failed with return code {result.returncode}")
            sys.exit(result.returncode)
            
    except Exception as e:
        print(f"\n❌ Error running E2E tests: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_e2e()
