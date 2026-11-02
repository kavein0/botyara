mod selection;
use rustclr::{RustClr, RuntimeVersion};
use std::{env, io::{self, Write}};

static CLIENT: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/BotyaraClient.exe"));

#[link(name = "bcrypt")]
extern "system" {
    fn BCryptGenRandom(algorithm: *mut std::ffi::c_void, buffer: *mut u8, length: u32, flags: u32) -> i32;
}

fn random_index() -> usize {
    loop {
        let mut byte = 0u8;
        let status = unsafe { BCryptGenRandom(std::ptr::null_mut(), &mut byte, 1, 2) };
        if status < 0 { eprintln!("Windows random generator failed: {status:#x}"); std::process::exit(1); }
        // Reject 255: exactly 85 equally likely values remain for each profile.
        if byte < 255 { return usize::from(byte % 3); }
    }
}

fn main() {
    let profile = env::var("BOT_PROFILE").ok();
    let config = env::var_os("BOT_CONFIG");
    let (chosen, reason) = selection::select(profile.as_deref(), config.as_ref().map(|_| "set"), random_index);
    if let Some(value) = chosen { env::set_var("BOT_PROFILE", value); }
    println!("Profile selection: {reason}; requested: {}", chosen.unwrap_or("standard if config is missing"));
    println!("Starting embedded Botyara client with CLR v4");
    io::stdout().flush().ok();
    let result = RustClr::new(CLIENT).and_then(|mut clr| {
        clr = clr.with_runtime_version(RuntimeVersion::V4);
        clr.run()
    });
    if let Err(error) = result {
        eprintln!("Cannot run embedded client (compatible .NET Framework required): {error}");
        std::process::exit(1);
    }
}
