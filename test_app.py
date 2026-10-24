import unittest
from datetime import date

from app import (
    AppError,
    DEFAULT_PRODUCTS,
    add_movement,
    authenticate_user,
    bootstrap_first_admin,
    hash_password,
    import_legacy_backup,
    format_time,
    parse_product,
    parse_quantity,
    peru_now,
    stock_by_product,
    validate_legacy_backup,
    validate_username,
    verify_password,
)


def record(record_id, fields):
    return {"id": record_id, "fields": fields}


class FakeAirtableClient:
    def __init__(self, products, movements, users=None):
        self.products = products
        self.movements = movements
        self.users = users or []

    def list_records(self, table):
        return {
            "Productos": self.products,
            "Movimientos": self.movements,
            "Usuarios": self.users,
        }[table]

    def create_record(self, table, fields):
        created = record(f"rec{len(self.movements) + 1}", fields)
        if table == "Usuarios":
            self.users.append(created)
        elif table == "Movimientos":
            self.movements.append(created)
        else:
            self.products.append(created)
        return created

    def create_records(self, table, records):
        for fields in records:
            self.create_record(table, fields)


class InventoryTests(unittest.TestCase):
    def test_peru_timezone_is_used_for_current_time_and_movement_time(self):
        now = peru_now()
        self.assertEqual(now.utcoffset().total_seconds(), -5 * 60 * 60)
        self.assertEqual(format_time("2026-10-24T09:05:06-05:00"), "09:05:06")
        self.assertEqual(format_time("2026-10-24"), "")

    def test_parse_product_and_quantity_without_misreading_package_size(self):
        self.assertEqual(parse_product("Barquimieles de 360 unidades"), "barquimieles-360")
        self.assertEqual(parse_product("Barquimieles de 12 unidades"), "barquimieles-12")
        self.assertEqual(parse_product("Coronitas"), "coronitas")
        self.assertIsNone(parse_quantity("Barquimieles de 360 unidades"))
        self.assertEqual(parse_quantity("Barquimieles de 360 unidades cantidad: 4"), 4)
        self.assertEqual(parse_quantity("3 cajas de Coronitas"), 3)

    def test_stock_is_derived_from_movements(self):
        products = [
            record("p1", {"ID": "barquimieles-360", "Nombre": "Barquimieles", "Activo": True}),
            record("p2", {"ID": "coronitas", "Nombre": "Coronitas", "Activo": True}),
        ]
        movements = [
            record("m1", {"ID": "m1", "Producto ID": "barquimieles-360", "Tipo": "Entrada", "Cantidad": 5, "Fecha": "2026-10-24"}),
            record("m2", {"ID": "m2", "Producto ID": "barquimieles-360", "Tipo": "Salida", "Cantidad": 2, "Fecha": "2026-10-24"}),
            record("m3", {"ID": "m3", "Producto ID": "coronitas", "Tipo": "Entrada", "Cantidad": 7, "Fecha": "2026-10-24"}),
        ]
        self.assertEqual(
            stock_by_product(products, movements),
            {"barquimieles-360": 3, "coronitas": 7},
        )

    def test_rejects_sale_beyond_fresh_airtable_stock(self):
        product = record(
            "p1",
            {"ID": "barquimieles-360", "Nombre": "Barquimieles", "Precio": 9, "Activo": True},
        )
        client = FakeAirtableClient(
            [product],
            [record("m1", {"ID": "m1", "Producto ID": "barquimieles-360", "Tipo": "Entrada", "Cantidad": 2, "Fecha": "2026-10-24"})],
        )
        with self.assertRaisesRegex(AppError, "Existencia insuficiente"):
            add_movement(
                client,
                movement_type="Salida",
                product_id="barquimieles-360",
                quantity=3,
                movement_date=date(2026, 10, 24),
                note="",
                username="worker",
            )
        self.assertEqual(len(client.movements), 1)

    def test_saves_price_snapshot_on_server(self):
        product = record(
            "p1",
            {"ID": "barquimieles-360", "Nombre": "Barquimieles", "Precio": 9, "Activo": True},
        )
        client = FakeAirtableClient([product], [])
        add_movement(
            client,
            movement_type="Entrada",
            product_id="barquimieles-360",
            quantity=2,
            movement_date=date(2026, 10, 24),
            note="",
            username="worker",
        )
        self.assertEqual(client.movements[0]["fields"]["Precio unitario"], 9)
        timestamp = client.movements[0]["fields"]["Fecha"]
        self.assertRegex(timestamp, r"^2026-10-24T\d{2}:\d{2}:\d{2}-05:00$")

    def test_passwords_are_salted_hashed_and_verified(self):
        first_hash = hash_password("correct horse battery staple")
        second_hash = hash_password("correct horse battery staple")
        self.assertNotEqual(first_hash, second_hash)
        self.assertTrue(verify_password("correct horse battery staple", first_hash))
        self.assertFalse(verify_password("wrong password", first_hash))

    def test_password_and_username_require_safe_lengths_and_format(self):
        with self.assertRaisesRegex(AppError, "12 caracteres"):
            hash_password("too-short")
        with self.assertRaisesRegex(AppError, "usuario debe tener"):
            validate_username("x")

    def test_authentication_uses_active_role_and_unique_username(self):
        password_hash = hash_password("worker password 123")
        users = [
            record("u1", {"Usuario": "worker", "Hash contraseña": password_hash, "Rol": "trabajador", "Activo": True}),
            record("u2", {"Usuario": "off", "Hash contraseña": password_hash, "Rol": "trabajador", "Activo": False}),
        ]
        self.assertEqual(authenticate_user("WORKER", "worker password 123", users), users[0])
        self.assertIsNone(authenticate_user("off", "worker password 123", users))
        self.assertIsNone(authenticate_user("worker", "wrong", users))
        duplicate_access = users + [
            record("u3", {"Usuario": "worker", "Hash contraseña": password_hash, "Rol": "admin", "Activo": True})
        ]
        self.assertIsNone(authenticate_user("worker", "worker password 123", duplicate_access))

    def test_first_admin_is_bootstrapped_with_only_password_hash(self):
        client = FakeAirtableClient([], [])
        users = bootstrap_first_admin(
            client, [], "Owner", "temporary admin password 123"
        )
        self.assertEqual(len(users), 1)
        fields = users[0]["fields"]
        self.assertEqual(fields["Usuario"], "owner")
        self.assertEqual(fields["Rol"], "admin")
        self.assertTrue(verify_password("temporary admin password 123", fields["Hash contraseña"]))
        self.assertNotEqual(fields["Hash contraseña"], "temporary admin password 123")
        self.assertNotIn("Correo", fields)

    def test_first_admin_can_be_added_alongside_legacy_email_accounts(self):
        legacy_user = record(
            "old-user",
            {"Correo": "old@example.com", "Rol": "admin", "Activo": True},
        )
        client = FakeAirtableClient([], [], [legacy_user])
        users = bootstrap_first_admin(
            client, [legacy_user], "owner", "temporary admin password 123"
        )
        self.assertEqual(len(users), 2)
        self.assertEqual(users[1]["fields"]["Rol"], "admin")
        self.assertTrue(
            verify_password(
                "temporary admin password 123",
                users[1]["fields"]["Hash contraseña"],
            )
        )

    def test_legacy_backup_is_validated_and_mapped_to_airtable(self):
        product_id, _ = DEFAULT_PRODUCTS[0]
        payload = {
            "version": 1,
            "prices": {product_id: 11.5},
            "movements": [
                {
                    "id": "old-1",
                    "type": "in",
                    "productId": product_id,
                    "quantity": 2,
                    "date": "2026-10-24",
                    "note": "prueba",
                    "unitPrice": 11.5,
                }
            ],
        }
        result = validate_legacy_backup(payload)
        self.assertEqual(result["movements"][0]["ID"], "old-1")
        self.assertEqual(result["movements"][0]["Precio unitario"], 11.5)

    def test_legacy_backup_rejects_negative_stock(self):
        with self.assertRaisesRegex(AppError, "existencias negativas"):
            validate_legacy_backup(
                {
                    "version": 1,
                    "prices": {},
                    "movements": [
                        {
                            "id": "old-1",
                            "type": "out",
                            "productId": "coronitas",
                            "quantity": 1,
                            "date": "2026-10-24",
                            "note": "",
                            "unitPrice": None,
                        }
                    ],
                }
            )

    def test_legacy_backup_rejects_duplicate_movement_ids(self):
        movement = {
            "id": "same-id",
            "type": "in",
            "productId": "coronitas",
            "quantity": 1,
            "date": "2026-10-24",
            "note": "",
            "unitPrice": None,
        }
        with self.assertRaisesRegex(AppError, "datos inválidos"):
            validate_legacy_backup(
                {"version": 1, "prices": {}, "movements": [movement, movement]}
            )

    def test_legacy_import_skips_existing_movements(self):
        payload = {
            "version": 1,
            "prices": {"barquimieles-360": 11.5},
            "movements": [
                {
                    "id": "old-1",
                    "type": "in",
                    "productId": "barquimieles-360",
                    "quantity": 2,
                    "date": "2026-10-24",
                    "note": "",
                    "unitPrice": 11.5,
                }
            ],
        }
        products = [
            record(
                "p1",
                {
                    "ID": "barquimieles-360",
                    "Nombre": "Barquimieles",
                    "Precio": 15,
                    "Activo": True,
                },
            )
        ]
        movements = [
            record(
                "m1",
                {
                    "ID": "old-1",
                    "Tipo": "Entrada",
                    "Producto ID": "barquimieles-360",
                    "Cantidad": 2,
                },
            )
        ]
        client = FakeAirtableClient(products, movements)
        created_products, created_movements = import_legacy_backup(
            client, payload, products, movements
        )
        self.assertEqual(created_products, 2)
        self.assertEqual(created_movements, 0)
        self.assertEqual(len(client.movements), 1)
        self.assertEqual(products[0]["fields"]["Precio"], 15)


if __name__ == "__main__":
    unittest.main()
