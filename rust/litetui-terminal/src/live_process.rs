//! Windows-only live child containment. Assign the SUSPENDED process before it
//! can launch descendants. Closing our non-inheritable job kills all members.
use std::{
    io,
    process::{Child, Command},
};

#[cfg(windows)]
mod platform {
    use super::*;
    use std::os::windows::{io::AsRawHandle, process::CommandExt};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE},
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectBasicAccountingInformation,
                JobObjectExtendedLimitInformation, QueryInformationJobObject,
                SetInformationJobObject, TerminateJobObject,
            },
            Threading::{CREATE_SUSPENDED, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
        },
    };

    // std::process::Child retains the process handle, not its primary thread
    // handle. NtResumeProcess resumes that process after job assignment; a
    // non-success NTSTATUS fails closed and the still-owned job is terminated.
    #[link(name = "ntdll")]
    unsafe extern "system" {
        fn NtResumeProcess(process: HANDLE) -> i32;
    }

    pub struct Job(HANDLE);
    impl Job {
        pub fn spawn(command: &mut Command) -> io::Result<(Self, Child)> {
            // SAFETY: null security/name makes a private non-inheritable job.
            let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }
            let job = Self(handle);
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            // SAFETY: live job handle, correctly sized initialized input struct.
            if unsafe {
                SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                    std::mem::size_of_val(&limits) as u32,
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            let mut child = command.creation_flags(CREATE_SUSPENDED).spawn()?;
            // SAFETY: Child owns this process handle; child has not run yet.
            if unsafe { AssignProcessToJobObject(handle, child.as_raw_handle()) } == 0 {
                let error = io::Error::last_os_error();
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
            // SAFETY: same valid suspended process, now contained in our job.
            let status = unsafe { NtResumeProcess(child.as_raw_handle()) };
            if status < 0 {
                job.terminate()?;
                child.wait()?;
                return Err(io::Error::other(format!(
                    "NtResumeProcess failed: {status:#x}"
                )));
            }
            Ok((job, child))
        }
        pub fn contains_pid(&self, pid: u32) -> io::Result<bool> {
            // A Windows venv redirector can launch the interpreter as its child.
            // Check the live process handle against this launch's private kernel job.
            unsafe {
                let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
                if process.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let mut member = 0;
                let checked = IsProcessInJob(process, self.0, &mut member);
                let error = (checked == 0).then(io::Error::last_os_error);
                CloseHandle(process);
                match error {
                    Some(error) => Err(error),
                    None => Ok(member != 0),
                }
            }
        }
        pub fn active(&self) -> io::Result<u32> {
            let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { std::mem::zeroed() };
            // SAFETY: live handle and initialized output buffer with exact size.
            if unsafe {
                QueryInformationJobObject(
                    self.0,
                    JobObjectBasicAccountingInformation,
                    (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    std::mem::size_of_val(&info) as u32,
                    std::ptr::null_mut(),
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(info.ActiveProcesses)
        }
        pub fn terminate(&self) -> io::Result<()> {
            // SAFETY: this private job contains only this invocation's child tree.
            if unsafe { TerminateJobObject(self.0, 1) } == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        }
    }
    impl Drop for Job {
        fn drop(&mut self) {
            // SAFETY: unique live job handle; kill-on-close handles all members.
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
#[cfg(not(windows))]
mod platform {
    use super::*;
    pub struct Job;
    impl Job {
        pub fn spawn(_: &mut Command) -> io::Result<(Self, Child)> {
            Err(io::Error::other(
                "Live child containment is currently Windows-only",
            ))
        }
        pub fn contains_pid(&self, _: u32) -> io::Result<bool> {
            Ok(false)
        }
        pub fn active(&self) -> io::Result<u32> {
            Ok(0)
        }
        pub fn terminate(&self) -> io::Result<()> {
            Ok(())
        }
    }
}
pub use platform::Job;
