# Botyara: Rust + CLR v4

Windows x64-лоудер с одним встроенным managed EXE под .NET Framework 4.0.
Готовый файл: `dist/Loader.exe`. Конфиги создаются в текущем рабочем каталоге.
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

Сначала запустите сервер проекта. Случайный профиль при каждом запуске:

```powershell
Remove-Item Env:BOT_PROFILE, Env:BOT_CONFIG -ErrorAction SilentlyContinue
.\clients\rustclr-loader\dist\Loader.exe
```

Выбор равновероятен между standard, mixed и host и происходит один раз перед
запуском CLR. Используется Windows BCryptGenRandom с отбрасыванием смещения
при переводе случайного байта в индекс. Сохраняются раздельные файлы
payload-standard.json, payload-mixed.json, payload-host.json.

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
Вывод клиента идёт непосредственно в консоль. Лоудер использует только
with_runtime_version(RuntimeVersion::V4); захват вывода, патч Exit и аргументы
не требуются. Ошибка запуска или клиента возвращает ненулевой код процесса.

## Проверки

Из корня репозитория:

```powershell
$env:BOT_TEST_DOTNET_EXE = (Resolve-Path .\clients\dotnet\bin\Release\net40\BotyaraClient.exe).Path
.\.venv\Scripts\python.exe clients/verify_clients.py --client dotnet --reuse
$env:BOT_TEST_DOTNET_EXE = (Resolve-Path .\clients\rustclr-loader\dist\Loader.exe).Path
.\.venv\Scripts\python.exe clients/verify_clients.py --client dotnet --reuse
.\.venv\Scripts\python.exe clients/rustclr-loader/verify_loader.py
```

Первый EXE можно отдельно собрать `dotnet build clients/dotnet/BotyaraClient.csproj -c Release`.
Последний скрипт проверяет единственный Loader.exe в чистом временном каталоге,
все четыре профиля и команды, неизменность повторно используемых конфигов,
приоритет настроек, фактические правила custom, ошибки конфига, случайные запуски,
загрузку Framework64/CLR в процессе и отсутствие отдельного процесса клиента.
Сервер и SQLite изолированы. Для standalone net40 и Loader также моделируются
503 до сохранения и после сохранения каждого результата: клиент повторяет
результат, не выполняя задачу заново, и подтверждает exit перед завершением.
Перебор всех профилей не зависит от случайных запусков.

Сетевые команды ограничены whoami, ip, exit. Pending-результат хранится в памяти
текущего процесса; восстановление после аварийного завершения не добавлялось.
