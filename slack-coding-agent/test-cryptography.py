#!/usr/bin/env python3
"""
Test script to verify cryptography package is properly installed and working.
"""

import sys

def test_cryptography_import():
    """Test that cryptography can be imported."""
    print("Test 1: Importing cryptography...")
    try:
        import cryptography
        print(f"  ✅ cryptography version {cryptography.__version__} imported successfully")
        return True
    except ImportError as e:
        print(f"  ❌ Failed to import cryptography: {e}")
        return False

def test_jwt_with_rsa():
    """Test JWT signing with RSA (requires cryptography)."""
    print("\nTest 2: JWT signing with RSA...")
    try:
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend

        # Generate test RSA key pair
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )
        
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )

        # Sign a test token
        payload = {"test": "data", "user": "bot"}
        token = jwt.encode(payload, private_pem, algorithm="RS256")
        
        print(f"  ✅ JWT signing works! Token: {token[:60]}...")
        
        # Verify the token
        public_key = private_key.public_key()
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        
        decoded = jwt.decode(token, public_pem, algorithms=["RS256"])
        print(f"  ✅ JWT verification works! Payload: {decoded}")
        return True
        
    except Exception as e:
        print(f"  ❌ JWT signing failed: {e}")
        return False

def test_github_jwt_format():
    """Test GitHub App JWT format (what the bot actually uses)."""
    print("\nTest 3: GitHub App JWT format...")
    try:
        import jwt
        import time
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend

        # Generate test key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )
        
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )

        # Create GitHub App JWT payload
        now = int(time.time())
        payload = {
            "iat": now,
            "exp": now + (10 * 60),  # 10 minutes
            "iss": "12345"  # Fake app ID
        }
        
        token = jwt.encode(payload, private_pem, algorithm="RS256")
        print(f"  ✅ GitHub App JWT format works! Token: {token[:60]}...")
        
        decoded = jwt.decode(token, options={"verify_signature": False})
        print(f"  ✅ Payload decoded: iat={decoded['iat']}, exp={decoded['exp']}, iss={decoded['iss']}")
        return True
        
    except Exception as e:
        print(f"  ❌ GitHub JWT format failed: {e}")
        return False

def test_all_required_packages():
    """Test that all required packages are available."""
    print("\nTest 4: Checking all required packages...")
    packages = [
        "cryptography",
        "jwt",
        "boto3",
        "yaml",
        "requests",
    ]
    
    all_ok = True
    for package in packages:
        try:
            if package == "yaml":
                __import__("yaml")
                module_name = "PyYAML"
            elif package == "jwt":
                __import__("jwt")
                module_name = "PyJWT"
            else:
                __import__(package)
                module_name = package
            print(f"  ✅ {module_name}")
        except ImportError:
            print(f"  ❌ {package} not found")
            all_ok = False
    
    return all_ok

def main():
    """Run all tests."""
    print("=" * 70)
    print("Cryptography Installation Verification Tests")
    print("=" * 70)
    
    results = []
    results.append(("Cryptography Import", test_cryptography_import()))
    results.append(("JWT with RSA", test_jwt_with_rsa()))
    results.append(("GitHub JWT Format", test_github_jwt_format()))
    results.append(("All Required Packages", test_all_required_packages()))
    
    print("\n" + "=" * 70)
    print("Test Results Summary")
    print("=" * 70)
    
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")
    
    all_passed = all(result[1] for result in results)
    
    print("\n" + "=" * 70)
    if all_passed:
        print("✅ ALL TESTS PASSED - Cryptography is working correctly!")
        print("=" * 70)
        return 0
    else:
        print("❌ SOME TESTS FAILED - Please check the errors above")
        print("=" * 70)
        return 1

if __name__ == "__main__":
    sys.exit(main())
