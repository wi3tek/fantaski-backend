import asyncio
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.config import settings

async def run_tests():
    admin_email = settings.INITIAL_ADMIN_EMAIL or "admin@example.com"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # We need to trigger the lifespan
        async with app.router.lifespan_context(app):
            print("1. Testing healthcheck and security headers...")
            res = await client.get("/health")
            assert res.status_code == 200, res.text
            assert res.headers.get("x-frame-options") == "DENY", "Missing X-Frame-Options: DENY"
            assert res.headers.get("x-content-type-options") == "nosniff", "Missing X-Content-Type-Options"
            print("Healthcheck & Security Headers OK:", res.json())

            print(f"2. Dev login with initial admin ({admin_email})...")
            res_admin = await client.post("/auth/dev-login", json={"email": admin_email, "name": "Admin User"})
            assert res_admin.status_code == 200, res_admin.text
            admin_data = res_admin.json()
            admin_token = admin_data["access_token"]
            assert admin_data["user"]["role"] == "ADMIN", f"Expected ADMIN, got {admin_data['user']['role']}"
            print("Admin login OK. Role is ADMIN.")

            print("3. Dev login with standard user...")
            # Clean up user role from previous runs to test default USER role
            await app.database["users"].delete_one({"email": "regular.user@example.com"})
            res_user = await client.post("/auth/dev-login", json={"email": "regular.user@example.com", "name": "Regular User"})
            assert res_user.status_code == 200, res_user.text
            user_data = res_user.json()
            user_token = user_data["access_token"]
            user_id = user_data["user"]["id"]
            assert user_data["user"]["role"] == "USER", f"Expected USER, got {user_data['user']['role']}"
            print("Regular user login OK. Role is USER.")

            print("4. Testing /auth/me...")
            res_me = await client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"})
            assert res_me.status_code == 200
            assert res_me.json()["email"] == "regular.user@example.com"
            print("/auth/me OK.")

            print("5. Regular user accessing /auth/users (should be 403)...")
            res_forbidden = await client.get("/auth/users", headers={"Authorization": f"Bearer {user_token}"})
            assert res_forbidden.status_code == 403, f"Expected 403, got {res_forbidden.status_code}"
            print("RBAC check OK: regular user received 403.")

            print("6. Admin accessing /auth/users...")
            res_users = await client.get("/auth/users", headers={"Authorization": f"Bearer {admin_token}"})
            assert res_users.status_code == 200
            users_list = res_users.json()
            assert len(users_list) >= 2
            print(f"Admin /auth/users OK. Total users: {len(users_list)}.")

            print("7. Unauthenticated user accessing /stats/standings (should be 401)...")
            res_standings_anon = await client.post(
                "/stats/standings",
                json={"season": "2024/2025", "category": "WC"}
            )
            assert res_standings_anon.status_code == 401, f"Expected 401, got {res_standings_anon.status_code}"
            print("RBAC check OK: unauthenticated user blocked from standings (401).")

            print("7b. Regular user accessing /stats/standings (should be 200)...")
            res_standings_user = await client.post(
                "/stats/standings",
                headers={"Authorization": f"Bearer {user_token}"},
                json={"season": "2024/2025", "category": "WC"}
            )
            assert res_standings_user.status_code == 200, f"Expected 200, got {res_standings_user.status_code}"
            print("RBAC check OK: regular user can access /stats/standings (200).")

            print("7c. Regular user accessing Fantasy prices /stats/athletes/with-prices (should be 403)...")
            res_prices_user = await client.get(
                "/stats/athletes/with-prices",
                headers={"Authorization": f"Bearer {user_token}"}
            )
            assert res_prices_user.status_code == 403, f"Expected 403, got {res_prices_user.status_code}"
            print("RBAC check OK: regular user blocked from Fantasy prices (403).")

            print("8. Admin promoting user to USER_LITE...")
            res_promo_lite = await client.put(
                f"/auth/users/{user_id}/role",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"role": "USER_LITE"}
            )
            assert res_promo_lite.status_code == 200, res_promo_lite.text
            assert res_promo_lite.json()["role"] == "USER_LITE"
            print("Role update OK: user promoted to USER_LITE.")

            print("8b. USER_LITE testing comparison (2 athletes -> 200, 3 athletes -> 403)...")
            res_relogin_lite = await client.post("/auth/dev-login", json={"email": "regular.user@example.com"})
            lite_token = res_relogin_lite.json()["access_token"]

            # 2 athletes -> OK
            res_search_2 = await client.post(
                "/stats/search",
                headers={"Authorization": f"Bearer {lite_token}"},
                json={"names": ["KRAFT Stefan", "ZCYLA Piotr"]}
            )
            assert res_search_2.status_code == 200, f"Expected 200, got {res_search_2.status_code}"
            print("LITE 2-athlete search OK (200).")

            # 3 athletes -> 403
            res_search_3 = await client.post(
                "/stats/search",
                headers={"Authorization": f"Bearer {lite_token}"},
                json={"names": ["KRAFT Stefan", "ZYLA Piotr", "KUBACKI Dawid"]}
            )
            assert res_search_3.status_code == 403, f"Expected 403, got {res_search_3.status_code}"
            print("LITE 3-athlete search blocked OK (403 limit enforced).")

            print("8c. Admin promoting regular user to USER_PRO...")
            res_promo = await client.put(
                f"/auth/users/{user_id}/role",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"role": "USER_PRO"}
            )
            assert res_promo.status_code == 200, res_promo.text
            assert res_promo.json()["role"] == "USER_PRO"
            print("Role update OK: user promoted to USER_PRO.")

            print("9. Re-login as promoted user and access /stats/standings...")
            res_relogin = await client.post("/auth/dev-login", json={"email": "regular.user@example.com"})
            assert res_relogin.json()["user"]["role"] == "USER_PRO"
            new_user_token = res_relogin.json()["access_token"]

            res_standings_pro = await client.post(
                "/stats/standings",
                headers={"Authorization": f"Bearer {new_user_token}"},
                json={"season": "2024/2025", "category": "WC"}
            )
            assert res_standings_pro.status_code == 200, f"Expected 200, got {res_standings_pro.status_code}"
            print("RBAC check OK: USER_PRO can access /stats/standings.")

            print("10. Attempt to demote initial admin (should be 400)...")
            admin_id = admin_data["user"]["id"]
            res_demote = await client.put(
                f"/auth/users/{admin_id}/role",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"role": "USER"}
            )
            assert res_demote.status_code == 400, f"Expected 400, got {res_demote.status_code}"
            print("Protected initial admin check OK: initial admin cannot be demoted.")

            print("\n ALL AUTH & RBAC BACKEND TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(run_tests())
