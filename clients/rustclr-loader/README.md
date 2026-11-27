# Botyara: Rust + CLR v4

Windows x64-лоудер с одним встроенным managed EXE под .NET Framework 4.0.
Готовый файл: `dist/Loader.exe`. Конфиги создаются в текущем рабочем каталоге.
Скачать лоудер и самостоятельный C#-клиент можно из
[последнего релиза](https://github.com/kavein0/botyara/releases/latest).
Для запуска нужен установленный совместимый **полный .NET Framework** (включая
System.Web.Extensions); .NET 10 не заменяет Framework. MSVC CRT связан статически.
Проверенная конфигурация выполнения: Framework 4.8.1 (Release 533509).
Запуск на исходном runtime Framework 4.0 и других версиях Windows не проверен.

## Сборка

Из корня репозитория:

```powershell
.\clients\rustclr-loader\build.ps1
```

Нужны .NET SDK (использован 10.0.401), Rust/Cargo для Windows x64 MSVC
(использован 1.99.1), Visual Studio Build Tools 2022 с MSVC x64 и Windows SDK.
Скрипт сначала ищет SDK в `$env:USERPROFILE/.dotnet`, Cargo — в
`../botyara-loader-downloads/toolchain/bin`, затем использует доступные команды.
Параметры `-Downloads`, `-Dotnet`, `-Cargo` позволяют указать другие пути.
Глобальный PATH не меняется. При наличии скачанного reference-пакета
восстановление net40 выполняется из папки загрузок, иначе — из NuGet-источников.
Первое получение зависимостей Cargo требует сети; последующие сборки используют
его кэш. `Cargo.lock` фиксирует зависимости; rustclr закреплён на commit
`9bdc8ef9bd0e7a6e4739a0b67d2c62d2933df90c` (0.3.4).

Каждая сборка принудительно пересобирает C# в новый каталог под `target`, затем
передаёт конкретный свежий EXE в build.rs через BOTYARA_CLIENT_EXE.
include_bytes! встраивает его в Loader. После сборки выполняются проверки
логики выбора профиля. Любая ошибка останавливает скрипт; `dist/Loader.exe`
обновляется только при успехе. После неудачной сборки в dist может остаться
последний успешный файл — не считайте его новым результатом.
`target` и `dist` исключены из Git.

## Запуск

Для клиентской части достаточно скопировать `Loader.exe` в папку с правом
записи. На Windows x64 с полным .NET Framework можно открыть файл двойным
щелчком. Python, .NET SDK, Rust, предварительный JSON и переменные `BOT_*` для
запуска клиента не нужны. Клиент по умолчанию подключается к серверу Botyara
по адресу `http://127.0.0.1:5000`. Сервер запускается отдельно.

Из корня репозитория:

```powershell
.\clients\rustclr-loader\dist\Loader.exe
```

По умолчанию лоудер выбирает один из трёх профилей с равной вероятностью и
передаёт его имя через `with_args` в `Main(string[] args)`. Правила `standard`,
`mixed` и `host` включены в C#-сборку внутри EXE. При первой регистрации клиент
отправляет серверу готовый объект правил и получает UUID. Затем создаёт
соответствующий файл `payload-standard.json`, `payload-mixed.json` или
`payload-host.json`.

Явные настройки:

```powershell
$env:BOT_SERVER = 'http://127.0.0.1:5000'
$env:BOT_PROFILE = 'mixed'
$env:BOT_CONFIG = 'C:\temp\botyara\my-client.json'
.\clients\rustclr-loader\dist\Loader.exe
```

Любая явно заданная BOT_PROFILE или BOT_CONFIG отключает случайный выбор.
При отсутствии файла используется BOT_PROFILE, а без него — standard.
Существующий конфиг всегда определяет фактические правила: BOT_PROFILE их
не перезаписывает. Пользовательские правила выводятся как custom, если не
совпадают с одним из встроенных профилей. `custom` не является допустимым
именем BOT_PROFILE: такой профиль задаётся объектом правил в конфиге,
созданном через серверный API. BOT_SERVER должен совпадать с server_url.
BOT_SLEEP и BOT_JITTER сохраняют прежний смысл.

Сообщение Profile selection сообщает источник выбора и запрошенный профиль;
Effective profile сообщает фактические правила после загрузки конфига.
Вывод клиента идёт непосредственно в консоль. Ошибка запуска или клиента
возвращает ненулевой код процесса.

## Проверки

Из корня репозитория:

```powershell
$env:BOT_TEST_DOTNET_EXE = (Resolve-Path .\clients\dotnet\bin\Release\net40\BotyaraClient.exe).Path
.\.venv\Scripts\python.exe clients/verify_clients.py --client dotnet --reuse
$env:BOT_TEST_DOTNET_EXE = (Resolve-Path .\clients\rustclr-loader\dist\Loader.exe).Path
.\.venv\Scripts\python.exe clients/verify_clients.py --client dotnet --reuse
.\.venv\Scripts\python.exe clients/rustclr-loader/verify_loader.py
.\.venv\Scripts\python.exe clients/rustclr-loader/verify_loader.py --default-port
```

Первый EXE можно отдельно собрать `dotnet build clients/dotnet/BotyaraClient.csproj -c Release`.
`verify_loader.py` проверяет единственный Loader.exe в чистом временном каталоге,
все четыре профиля и команды, неизменность повторно используемых конфигов,
приоритет настроек, фактические правила custom, ошибки конфига, случайные запуски,
загрузку Framework64/CLR в процессе и отсутствие отдельного процесса клиента.
Тестовый сервер принимает объект правил и для встроенных профилей возвращает
только UUID. Режим `--default-port` требует свободный порт 5000 и проверяет
запуск без переменных `BOT_*` и со стандартным системным `PATH`.
Сервер и SQLite изолированы. Для standalone net40 и Loader также моделируются
503 до сохранения и после сохранения каждого результата: клиент повторяет
результат, не выполняя задачу заново, и подтверждает exit перед завершением.
Перебор всех профилей не зависит от случайных запусков.

Сетевые команды ограничены whoami, ip, exit. Pending-результат хранится в памяти
текущего процесса; восстановление после аварийного завершения не добавлялось.
