# Registro compartido de barquillos

Aplicación con Streamlit y Airtable para que trabajadores registren entradas y salidas mientras el administrador controla productos, precios, accesos, reportes y respaldos. Cada persona inicia sesión con su propio usuario y contraseña; la app guarda únicamente hashes de contraseñas con sal, no contraseñas legibles. El token de Airtable vive solo en los secretos del servidor.

## Seguridad y alcance

- Los trabajadores inician sesión con una cuenta individual autorizada, ven existencias y registran movimientos. No ven precios, reportes, administración ni respaldos.
- El administrador inicial configura `app.bootstrap_admin_username` y `app.bootstrap_admin_password` en secretos del servidor. Después puede mantener productos y precios, autorizar/desactivar trabajadores, restablecerles contraseñas, consultar reportes semanales y descargar un respaldo.
- La app comprueba el usuario, el hash de contraseña, su rol y si la cuenta sigue activa. Las contraseñas requieren al menos 12 caracteres y se guardan como hashes PBKDF2 con sal. Los intentos fallidos se limitan temporalmente por sesión; cambiar o restablecer la contraseña invalida las sesiones anteriores. El token personal de Airtable nunca se entrega al navegador.
- Mantén la base de Airtable privada y limita el acceso directo a sus colaboradores administradores. Los trabajadores deben entrar por la app, no como colaboradores de la base.
- Los movimientos conservan el precio vigente al registrarse. Las salidas se vuelven a comprobar contra el inventario de Airtable antes de escribir.
- La fecha predeterminada, la hora de cada movimiento, los reportes y el nombre/fecha del respaldo usan la zona `America/Lima`. Airtable guarda fecha y hora en el campo de texto `Fecha`; los movimientos históricos que solo tengan fecha siguen siendo compatibles.
- En cada operación puedes registrar hasta 10 lotes, cada uno con su cantidad de cajas; todas las filas comparten un identificador de grupo para consultar la operación completa. Las existencias se desglosan por lote y, en una salida, cada caja se descuenta del lote seleccionado; la app bloquea cantidades superiores a las existencias de ese lote. Los campos `Lote` y `Grupo ID` deben existir en `Movimientos`.
- Airtable no ofrece transacciones para esta API. La app serializa las escrituras de inventario dentro del proceso de Streamlit, pero una instalación con varias réplicas independientes no puede garantizar una reserva de stock atómica. Para una operación con múltiples servidores o muchas ventas simultáneas se requiere una base transaccional como backend.
- La foto se sube al servidor de Streamlit para procesarse con Tesseract en español; no se guarda en Airtable y se retira del estado de captura tras leerla. El OCR solo propone producto/cantidad; un trabajador debe revisar y confirmar el formulario.
- Los registros no se cargan hasta configurar Airtable y publicar la aplicación. La app anterior guardaba datos únicamente en el navegador; esos registros no se transfieren automáticamente.

## 1. Preparar la base de Airtable

Crea una base y tres tablas con estos nombres y campos. Los nombres de campo deben coincidir exactamente. En `Usuarios`, usa `Usuario` y `Hash contraseña` (no los campos de correo de una versión antigua):

**Productos**

| Campo | Tipo recomendado |
| --- | --- |
| `ID` | Texto de una línea |
| `Nombre` | Texto de una línea |
| `Precio` | Moneda o número, con hasta dos decimales |
| `Activo` | Casilla de verificación |

**Movimientos**

| Campo | Tipo recomendado |
| --- | --- |
| `ID` | Texto de una línea |
| `Tipo` | Texto de una línea |
| `Producto ID` | Texto de una línea |
| `Producto` | Texto de una línea |
| `Cantidad` | Número entero |
| `Lote` | Texto de una línea |
| `Grupo ID` | Texto de una línea |
| `Fecha` | Texto de una línea; la app guarda fecha y hora ISO 8601 de Perú (`America/Lima`, UTC-05:00) |
| `Precio unitario` | Moneda o número, con hasta dos decimales |
| `Nota` | Texto de una línea |
| `Registrado por` | Texto de una línea |

**Usuarios**

| Campo | Tipo recomendado |
| --- | --- |
| `Usuario` | Texto de una línea |
| `Hash contraseña` | Texto de una línea |
| `Rol` | Texto de una línea (`admin` o `trabajador`) |
| `Activo` | Casilla de verificación |

Al entrar por primera vez con una cuenta administrativa, la app agrega los tres productos iniciales si `Productos` está vacía. No les asigna precio.

Si ya habías creado `Usuarios` con el esquema anterior que usaba `Correo`, agrega `Usuario` y `Hash contraseña`; puedes dejar `Correo` sin utilizar. Los accesos viejos no se convierten en contraseñas: la app crea una cuenta administradora con las credenciales iniciales y después tendrás que dar de alta nuevamente a los trabajadores.

En la configuración de Airtable crea un **Personal Access Token** con `data.records:read` y `data.records:write`, limitado únicamente a esta base. Copia el ID de base que empieza con `app...`. No uses una API key antigua ni pegues el token en el código.

## 2. Probar en tu equipo

Necesitas Python 3.10 o posterior, Tesseract OCR y datos de idioma español. En Windows instala Tesseract con el paquete de idioma español; `packages.txt` hace esta instalación automáticamente en Streamlit Community Cloud.

En PowerShell, desde esta carpeta:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
```

Edita `.streamlit\secrets.toml` con el token de Airtable y una cuenta administrativa temporal. Usa una contraseña temporal única de al menos 12 caracteres; guárdala solo en los secretos del servidor. Conserva los secretos fuera de Git. No escribas claves en `.streamlit\secrets.toml.example`, que solo es una plantilla. Luego inicia la app:

```powershell
streamlit run app.py
```

## 3. Publicar para tus trabajadores

Streamlit Community Cloud despliega desde un repositorio GitHub. Sube estos archivos a un repositorio privado, crea una app apuntando a `app.py` y carga los valores de `.streamlit\secrets.toml.example` en **App settings → Secrets**. No subas `.streamlit/secrets.toml` ni contraseñas reales al repositorio.

Al abrir por primera vez, la app crea la cuenta administradora con las credenciales temporales del secreto. Inicia sesión y cámbiala desde **Cambiar mi contraseña**; luego elimina `bootstrap_admin_username` y `bootstrap_admin_password` de los secretos y vuelve a iniciar la app. En **Usuarios**, asigna a cada trabajador un nombre de usuario y una contraseña inicial de al menos 12 caracteres; comunícaselos de forma privada. Cada trabajador puede cambiar su propia contraseña. Desactiva su acceso desde el mismo panel cuando ya no trabaje contigo.

Si pierdes el acceso de administrador, configura un usuario y una contraseña temporal nuevos en `app.bootstrap_admin_username` y `app.bootstrap_admin_password`, y activa `app.reset_admin_password = true` en Secrets. La app cambiará únicamente el hash de la cuenta administradora cuyo usuario coincida. Después, desactiva `app.reset_admin_password` en Secrets antes de iniciar sesión y cambiar la contraseña; mientras esté activo, la contraseña temporal seguirá teniendo prioridad. Si la cuenta está desactivada, primero reactívala en Airtable.

## Uso y respaldos

- El panel de trabajadores sirve para consultar cantidades y registrar entradas/salidas manualmente o desde una propuesta de foto. Cada fila debe incluir el código de lote y su cantidad; una operación puede contener varios lotes.
- El administrador configura precios y catálogo, consulta el reporte de cada semana y descarga CSV.
- **Airtable y respaldos** permite descargar un JSON con los datos actuales. La app no elimina movimientos; desactiva productos o personas para conservar el historial.
- El OCR requiere `tesseract-ocr` y el idioma `spa`. Reconoce texto impreso como `Barquimieles` o el alias confirmado `Barquiroll Premium`, y propone el código cuando aparece junto a `Lote`. `Contenido: 360 UND` identifica la presentación, no el número de presentaciones en inventario. La cantidad del movimiento debe aparecer indicada por separado; si no se reconoce, complétala en la tabla de lotes. La calidad depende de iluminación, nitidez y texto legible; siempre revisa y confirma la propuesta.
- El formato monetario predeterminado es MXN; se puede cambiar `app.currency` en los secretos.

## Migrar registros de la app local anterior

Si ya habías registrado información en la versión del navegador, no la borres: esa información sigue en el almacenamiento local de ese navegador. Abre `index.html` en el mismo navegador y ruta para descargar un respaldo JSON. Después, ya como administrador, ve a **Airtable y respaldos**, carga el archivo y confirma la importación. Los movimientos con el mismo ID no se duplican; los precios de productos que ya existen en Airtable se conservan.

Una vez migrados los datos, deja de usar la versión local: no tiene cuentas ni permisos.
