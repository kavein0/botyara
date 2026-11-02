fn main() {
    println!("cargo:rerun-if-env-changed=BOTYARA_CLIENT_EXE");
    let path = std::env::var("BOTYARA_CLIENT_EXE")
        .expect("Use build.ps1: BOTYARA_CLIENT_EXE must name a freshly built net40 client");
    let path = std::path::Path::new(&path).canonicalize().expect("Client EXE missing");
    let bytes = std::fs::read(&path).expect("Cannot read client EXE");
    assert!(bytes.starts_with(b"MZ"), "Client is not a PE executable");
    println!("cargo:rerun-if-changed={}", path.display());
    std::fs::write(std::path::Path::new(&std::env::var("OUT_DIR").unwrap()).join("BotyaraClient.exe"), bytes).unwrap();
}
