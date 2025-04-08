# EdyBot

EdyBot is a simple chatbot application that was designed at Espace des Inventions (EDI) for PâKOMUZé 2025. It uses Gemini 2.5 Flash and Tkinter.

The user talks with "Edy", a bot which has to protect a code from the humans, and can only give the code to the robots. The user must answer questions or send pictures of themselves (disguised as robots) to convince Edy that they are robots.

When convinced, Edy gives them the code and prints the valid image (if there is one) to the default printer. A new session is then started after 30s.

## Prerequisites

- Install **Python 3.11**

## Installation and Setup

1. **Set Execution Policy**  
    Open PowerShell as Administrator and run the following command to set the execution policy to `RemoteSigned`:
    ```powershell
    Set-ExecutionPolicy RemoteSigned
    ```

2. **Create the Environment**  
    Run the `create_env.ps1` script to set up the required environment. 
    

3. **Add your API key**
    Create a `.env` file in the root directory of the project and add the following line:

    ```
    GOOGLE_API_KEY=<key>
    ```

    You can obtain your API key from [Google AI Studio](https://aistudio.google.com/apikey).
    ```powershell
    .\create_env.ps1
    ```

    > [!CAUTION]  
    > Never share your API Keys (especially in source control. We've added the `.env` file to the `.gitignore` by default as a precaution)
4. **Copy the Shortcut**  
    Once the environment is created, you can copy the `EdyBot.Ink` shortcut to any location you prefer.

5. **Modify Paths (Optional)**  
    If needed, you can modify the path to the executable in the following files:
    - `edybot_launch.ps1`
    - `edybot_launch_training.ps1`
    - `EdyBot.Ink`
    - `EdyBot_Training.Ink`

## Usage

- Double-click the `EdyBot.Ink` shortcut to launch EdyBot.
- For training purposes, use the `EdyBot_Training.Ink` shortcut.

## Notes

- Ensure all dependencies are installed and paths are correctly configured for smooth operation.
- If you encounter any issues, verify the execution policy and environment setup.