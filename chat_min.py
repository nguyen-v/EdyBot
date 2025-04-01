import customtkinter as ctk
import tkinter.messagebox as messagebox  # for error popups if needed
import tkinter as tk
import cv2
import threading
import queue
import os
from google import genai
from google.genai.types import Part, GenerateContentConfig, SafetySetting
from dotenv import load_dotenv
from PIL import Image, ImageTk, ImageWin
import time
import win32print
import win32ui
import win32con
import tempfile
import io
import re
from datetime import datetime

# --- Configuration ---
PHOTOS_DIR = "photos_tk"
if not os.path.exists(PHOTOS_DIR):
    os.makedirs(PHOTOS_DIR)

load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")
if not API_KEY:
    print("ERROR: GOOGLE_API_KEY environment variable not set.")
    exit()

try:
    client = genai.Client(api_key=API_KEY)
    MODEL_ID = "gemini-2.0-flash-exp"
    safety_settings = [
        SafetySetting(category=f"HARM_CATEGORY_{c}", threshold="BLOCK_NONE")
        for c in ["DANGEROUS_CONTENT", "HARASSMENT", "HATE_SPEECH", "SEXUALLY_EXPLICIT"]
    ]

    def create_gemini_chat(model_id_to_use, settings):
        """Helper function to create a new Gemini chat session."""
        return client.chats.create(
            model=model_id_to_use,
            config=GenerateContentConfig(safety_settings=settings, temperature=1)
        )

    chat_session = create_gemini_chat(MODEL_ID, safety_settings)
    print(f"Gemini Client and Chat Session ({MODEL_ID}) initialized.")

except Exception as e:
    print(f"ERROR: Failed to initialize Gemini Client/Chat: {e}")
    exit()
# --- End Configuration ---

# --- Styling Constants ---
BASE_FONT_SIZE = 16  # Increased font size
APP_FONT = ('Helvetica', BASE_FONT_SIZE)
APP_FONT_BOLD = ('Helvetica', BASE_FONT_SIZE, 'bold')

BUBBLE_PADX = 10  
BUBBLE_PADY = 5
BUBBLE_MARGIN_X = 10
BUBBLE_MARGIN_Y = 5
USER_BUBBLE_COLOR = "#007bff"  # Blue for user bubbles

# Define dark gray for bot bubbles and camera frame background
DARK_GRAY = "#555555"  
# For Edy (bot) messages, use dark gray bubble and white text.
EDDY_BUBBLE_COLOR = DARK_GRAY
EDDY_TEXT_COLOR = "white"
# For user messages, keep existing colors.
USER_TEXT_COLOR = "white"

# Set max bubble width factor to 0.5 (half of chat pane width)
MAX_BUBBLE_WIDTH_FACTOR = 0.5

# --- Helper Function for Auto-scroll ---
def find_canvas(widget):
    """Recursively searches for a tk.Canvas within a widget's descendants."""
    if isinstance(widget, tk.Canvas):
        return widget
    for child in widget.winfo_children():
        result = find_canvas(child)
        if result:
            return result
    return None

def remove_analysis_blocks(text):
    pattern = r"ANALYSIS_START.*?ANALYSIS_END[\s\r\n]*"
    return re.sub(pattern, "", text, flags=re.DOTALL)
    # return text

class MinimalChatApp:
    def __init__(self, root, initial_chat_session, client_ref, model_id_ref, safety_settings_ref):
        self._bubbles = []  # store references to all (frame, label) for bubble
        self.root = root
        self.chat_session = initial_chat_session
        self.client = client_ref
        self.model_id = model_id_ref
        self.safety_settings = safety_settings_ref
        self.root.title(f"Minimal EdyBot ({self.model_id} - CustomTkinter)")
        # self.root.geometry("1920x1080")  # Slightly larger default window
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        self.root.geometry(f"{screen_width}x{screen_height}")

        # --- State ---
        self.attached_image_pil = None
        self.image_sent_with_last_api_call = None
        self.latest_frame_cv = None
        self.camera_running = True
        self.frame_queue = queue.Queue(maxsize=5)
        self.response_queue = queue.Queue()
        self._chat_image_references = []

        # --- Layout ---
        self._setup_ui()

        # --- Initialization ---
        self.start_camera_thread()
        self.update_camera_feed()
        self.process_response_queue()
        self.load_initial_prompt()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.after(100, lambda: self.root.state('zoomed'))

    def _setup_ui(self):
        """Creates and arranges the UI elements using CustomTkinter widgets."""
        # Main container
        # Replace your main_frame creation with a PanedWindow.
        paned_window = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg="#222222", sashwidth=5)
        paned_window.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Create the left frame as a CTkFrame and add it to the paned window.
        left_frame = ctk.CTkFrame(paned_window, fg_color="#222222")
        paned_window.add(left_frame, minsize=1200)  # set a minimum size if needed

        # Create the right frame as a CTkFrame and add it to the paned window.
        right_frame = ctk.CTkFrame(paned_window, fg_color="#222222")
        paned_window.add(right_frame, minsize=200)

        self.left_frame = left_frame  # Save for later use in attach_image
        # --- Left Frame ---
        # Chat area using a scrollable frame
        self.scrollable_frame = ctk.CTkScrollableFrame(
            left_frame, corner_radius=0, label_text=None, fg_color="#222222"
        )
        self.scrollable_frame.pack(side=ctk.TOP, fill=ctk.BOTH, expand=True, pady=0)
        self.chat_canvas = self.scrollable_frame._parent_canvas  # save reference to internal canvas

        # Input area (BOTTOM)
        input_frame = ctk.CTkFrame(left_frame, fg_color="#222222")
        input_frame.pack(side=ctk.BOTTOM, fill=ctk.X, pady=0)  # No vertical padding
        self.message_entry = ctk.CTkEntry(input_frame, width=50, font=APP_FONT)
        self.message_entry.pack(side=ctk.LEFT, fill=ctk.X, expand=True, padx=(0, 5))
        self.message_entry.bind("<Return>", self.send_message)

        self.send_button = ctk.CTkButton(
            input_frame, text="Envoyer", command=self.send_message, font=APP_FONT
        )
        self.send_button.pack(side=ctk.RIGHT)

        # Thumbnail preview: initially do not pack this widget
        # self.thumbnail_label = ctk.CTkLabel(left_frame, text="", font=APP_FONT, fg_color=DARK_GRAY)

        # --- Right Frame ---
        # Camera preview area with dark gray background
        self.camera_label = ctk.CTkLabel(right_frame, text="Starting Camera...", fg_color=DARK_GRAY,
                                        font=APP_FONT, justify="center")
        self.camera_label.pack(fill=ctk.BOTH, expand=True, pady=(0, 5))

        # Camera control buttons
        camera_buttons_frame = ctk.CTkFrame(right_frame)
        camera_buttons_frame.pack(fill=ctk.X)
        self.attach_button = ctk.CTkButton(camera_buttons_frame, text="Attacher une Image",
                                           command=self.attach_image, font=APP_FONT)
        self.attach_button.pack(side=ctk.LEFT, expand=True, fill=ctk.X)

    def _scroll_to_bottom(self):
        """Scrolls the chat area to the bottom using the internal canvas."""
        if self.chat_canvas:
            self.chat_canvas.update_idletasks()
            self.chat_canvas.yview_moveto(1.0)

    def _scroll_to_top(self):
        """Scrolls the chat area to the top using the internal canvas."""
        if self.chat_canvas:
            self.chat_canvas.update_idletasks()
            self.chat_canvas.yview_moveto(0.0)

    # --- Camera Handling ---
    def _camera_thread_func(self):
        """Function run in the camera thread to capture frames."""
        cap = None
        try:
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            if not cap.isOpened():
                self.response_queue.put("SYSTEM_ERROR: Could not open camera.")
                return

            while self.camera_running:
                ret, frame = cap.read()
                if ret:
                    self.latest_frame_cv = frame.copy()
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    try:
                        self.frame_queue.put_nowait(rgb_frame)
                    except queue.Full:
                        try:
                            self.frame_queue.get_nowait()
                            self.frame_queue.put_nowait(rgb_frame)
                        except queue.Empty:
                            pass
                else:
                    self.response_queue.put("SYSTEM_ERROR: Failed to read frame.")
                    time.sleep(1)

                time.sleep(0.03)
        except Exception as e:
            self.response_queue.put(f"SYSTEM_ERROR: Camera Thread Error: {e}")
        finally:
            if cap and cap.isOpened():
                cap.release()
            print("Camera thread stopped.")

    def start_camera_thread(self):
        """Starts the camera thread."""
        self.camera_thread = threading.Thread(target=self._camera_thread_func, daemon=True)
        self.camera_thread.start()

    def update_camera_feed(self):
        """Periodically checks the frame queue and updates the camera preview."""
        try:
            while not self.frame_queue.empty():
                rgb_frame = self.frame_queue.get_nowait()
                img = Image.fromarray(rgb_frame)
                label_w = self.camera_label.winfo_width()
                label_h = self.camera_label.winfo_height()
                if label_w > 1 and label_h > 1:
                    img.thumbnail((label_w, label_h), Image.Resampling.LANCZOS)

                imgtk = ImageTk.PhotoImage(image=img)
                self.camera_label.imgtk = imgtk
                self.camera_label.configure(image=imgtk, text="")  # remove text when image is available
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Error updating camera feed: {e}")
            self.camera_label.configure(image='', text=f"Feed Error:\n{e}")

        if self.camera_running:
            self.root.after(30, self.update_camera_feed)

    # --- Chat Display ---
    def add_message(self, sender, message, img_pil=None):
        if sender == "System":
            print(f"System: {message}")
            return

        if sender == "Edy":
            bubble_color = EDDY_BUBBLE_COLOR
            text_color = EDDY_TEXT_COLOR
        else:
            bubble_color = USER_BUBBLE_COLOR
            text_color = USER_TEXT_COLOR

        align = "w" if sender == "Edy" else "e"

        # Outer frame for alignment and margins
        outer_frame = ctk.CTkFrame(self.scrollable_frame, fg_color=self.scrollable_frame.cget("fg_color"))
        outer_frame.pack(fill="x", padx=BUBBLE_MARGIN_X, pady=BUBBLE_MARGIN_Y, anchor=align)

        # Inner frame is the visual bubble
        bubble_frame = ctk.CTkFrame(outer_frame, fg_color=bubble_color, corner_radius=10)
        bubble_frame.pack(anchor=align, padx=5, pady=2)

        # For Edy messages, add the "Edy" label inside the bubble (above the message text)
        if sender == "Edy":
            edy_label = ctk.CTkLabel(
                bubble_frame,
                text="Edy",
                font=APP_FONT_BOLD,
                text_color=EDDY_TEXT_COLOR
            )
            # Added padx for padding so that "Edy" is not flush with the bubble's left edge.
            edy_label.pack(anchor="w", padx=BUBBLE_PADX, pady=(0, 2))

        # Prepare image (if any) for inclusion in the bubble
        imgtk = None
        compound_pos = "none"
        if img_pil:
            try:
                img_copy = img_pil.copy()
                img_copy.thumbnail((200, 200), Image.Resampling.LANCZOS)
                imgtk = ImageTk.PhotoImage(img_copy)
                self._chat_image_references.append(imgtk)
                compound_pos = "top"
            except Exception as e:
                print(f"Error processing image for chat bubble: {e}")
                imgtk = None

        # Message text label inside the bubble
        canvas_width = 1000
        wrap_width = int(canvas_width * MAX_BUBBLE_WIDTH_FACTOR)
        bubble_label = ctk.CTkLabel(
            bubble_frame,
            text=message if message else "",
            font=APP_FONT,
            text_color=text_color,
            wraplength=wrap_width,
            justify="left",
            compound=compound_pos,
            image=imgtk
        )
        bubble_label.pack(padx=BUBBLE_PADX, pady=BUBBLE_PADY, fill="both", expand=True)

        self.root.after(50, self._scroll_to_bottom)


    # --- Image Attachment ---
    def attach_image(self):
        """Captures the current camera frame and updates the thumbnail with a cancel button."""
        if self.latest_frame_cv is not None:
            try:
                rgb_frame = cv2.cvtColor(self.latest_frame_cv, cv2.COLOR_BGR2RGB)
                self.attached_image_pil = Image.fromarray(rgb_frame)
                print("Image attached by user.")

                img_copy = self.attached_image_pil.copy()
                img_copy.thumbnail((150, 100), Image.Resampling.LANCZOS)
                imgtk = ImageTk.PhotoImage(img_copy)
                
                # Remove any existing thumbnail frame
                if hasattr(self, "thumbnail_frame") and self.thumbnail_frame is not None:
                    self.thumbnail_frame.destroy()
                
                # Create a new frame to contain the thumbnail and the cancel button
                self.thumbnail_frame = ctk.CTkFrame(self.left_frame, fg_color=DARK_GRAY)
                self.thumbnail_frame.pack(side=ctk.BOTTOM, fill=ctk.X, pady=(5, 0))
                
                # Create the thumbnail image label
                self.thumbnail_label = ctk.CTkLabel(
                    self.thumbnail_frame,
                    image=imgtk,
                    text="",
                    font=APP_FONT,
                    fg_color=DARK_GRAY
                )
                self.thumbnail_label.image = imgtk  # Keep a reference
                self.thumbnail_label.pack(side=ctk.LEFT)
                
                # Create the cancel (cross) button at the top right of the thumbnail frame
                self.cancel_button = ctk.CTkButton(
                    self.thumbnail_frame,
                    text="X",
                    width=20,
                    height=20,
                    font=("Helvetica", 10),
                    command=self._clear_attachment_state
                )
                # Position the cancel button at the top-right corner
                self.cancel_button.place(relx=1.0, rely=0.0, anchor="ne")
                
                # --- NEW: Scroll chat canvas upward so messages aren't hidden ---
                if self.chat_canvas:
                    self._scroll_to_bottom()
                
            except Exception as e:
                print(f"System: Error attaching image: {e}")
                self._clear_attachment_state()
        else:
            print("System: No camera frame available to attach.")

    def _clear_attachment_state(self):
        """Resets the currently attached image state and hides the thumbnail."""
        self.attached_image_pil = None
        if hasattr(self, "thumbnail_frame") and self.thumbnail_frame is not None:
            self.thumbnail_frame.destroy()
            self.thumbnail_frame = None


    # --- Gemini Interaction ---
    def _send_to_gemini_thread_func(self, parts):
        """Sends message parts to the Gemini API in a separate thread."""
        try:
            response = self.chat_session.send_message(message=parts)
            response_text = response.text
        except Exception as e:
            response_text = f"SYSTEM_ERROR: Gemini API Error: {e}"
            print(f"Gemini API Error: {e}")
        self.response_queue.put(response_text)

    def process_response_queue(self):
        """Checks queue for Gemini responses or errors and updates UI/logs."""
        try:
            response_text = self.response_queue.get_nowait()
            if response_text.startswith("SYSTEM_ERROR:"):
                print(f"System: {response_text.replace('SYSTEM_ERROR:', '').strip()}")
            else:
                cleaned_response = remove_analysis_blocks(response_text)
                self.add_message("Edy", cleaned_response)
                if "MOZZARELLA" in response_text:
                    print("Password 'MOZZARELLA' detected in response!")
                    print("System: Password detected! Initiating print and reset sequence...")
                    image_to_print_now = self.image_sent_with_last_api_call
                    if image_to_print_now:
                        self.root.after(100, lambda img=image_to_print_now: self.print_image(image_override=img))
                    else:
                        print("System: Password detected, but no image was sent with the triggering message.")
                    self.root.after(30000, self.reset_chat)
                    self.image_sent_with_last_api_call = None
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Error processing response queue: {e}")

        self.root.after(100, self.process_response_queue)

    def send_message(self, event=None):
        """Prepares and sends a user message (text/image) to Gemini."""
        user_text = self.message_entry.get().strip()
        current_attached_image = self.attached_image_pil

        if not user_text and current_attached_image is None:
            return

        message_parts = []
        display_img_copy = current_attached_image.copy() if current_attached_image else None
        self.add_message("You", user_text, img_pil=display_img_copy)

        self.image_sent_with_last_api_call = None
        if current_attached_image:
            try:
                img_byte_arr = io.BytesIO()
                rgb_image = current_attached_image
                if rgb_image.mode != 'RGB':
                    rgb_image = rgb_image.convert('RGB')
                rgb_image.save(img_byte_arr, format='JPEG', quality=90)
                img_bytes = img_byte_arr.getvalue()
                message_parts.append(Part.from_bytes(img_bytes, mime_type="image/jpeg"))
                self.image_sent_with_last_api_call = current_attached_image.copy()
                print(f"Image part prepared ({len(img_bytes)} bytes). Context stored.")
            except Exception as e:
                print(f"System: Error processing image for sending: {e}")
                self._clear_attachment_state()
                self.image_sent_with_last_api_call = None
                return

        if user_text:
            message_parts.append(Part(text=user_text))
            print(f"Text part prepared: '{user_text}'")

        self.message_entry.delete(0, ctk.END)
        self._clear_attachment_state()

        if message_parts:
            api_thread = threading.Thread(target=self._send_to_gemini_thread_func,
                                          args=(message_parts,), daemon=True)
            api_thread.start()
        else:
            print("Warning: No valid parts generated to send to API.")

    # --- Printing ---
    def print_image(self, image_override):
        """Prints the provided image using Windows default printer."""
        image_to_print = image_override
        if image_to_print is None:
            print("Print Error: No image provided to print function.")
            return

        print(f"Starting print process for image (id: {id(image_to_print)})...")
        temp_path = None
        hDC = None
        try:
            printer_name = win32print.GetDefaultPrinter()
            if not printer_name:
                print("Print Error: No default printer found.")
                return
            print(f"Using printer: {printer_name}")

            # --- Logo Addition Section (Corrected) ---
            try:
                # Load the logo
                logo_path = "Espace-des-inventions_logo.png" # Make sure this path is correct
                logo = Image.open(logo_path)

                # <<< CHANGE 1: Ensure logo is in RGBA mode >>>
                logo = logo.convert("RGBA")

                # <<< CHANGE 2: Ensure base image is in RGBA mode for pasting >>>
                if image_to_print.mode != 'RGBA':
                    image_to_print = image_to_print.convert('RGBA')

                # Resize logo (using modern resampling if available)
                logo_max_width = int(image_to_print.width * 0.2) # Logo width as 20% of image width
                logo_aspect = logo.width / logo.height
                logo_width = min(logo.width, logo_max_width)
                logo_height = int(logo_width / logo_aspect)
                # Use Image.Resampling.LANCZOS if available (Pillow >= 9.1.0), else fallback
                resampling_filter = getattr(Image, "Resampling", Image).LANCZOS
                logo = logo.resize((logo_width, logo_height), resampling_filter)

                # Create a copy to paste onto (already RGBA)
                combined_image = image_to_print.copy()

                # <<< CHANGE 3: Paste using the logo's alpha channel directly >>>
                # Pillow's paste uses the alpha channel of the source image (logo)
                # automatically when pasting onto an RGBA image if the mask argument
                # is the source image itself.
                paste_position = (100, 30) # Top-left corner with 20px padding
                combined_image.paste(logo, paste_position, logo) # Use logo as mask

                # Replace the original image reference with the combined image (still RGBA)
                image_to_print = combined_image
                print("Logo added to the image with transparency preserved.")

            except FileNotFoundError:
                 print(f"Warning: Logo file not found at {logo_path}. Skipping logo addition.")
            except Exception as logo_err:
                print(f"Warning: Could not add logo: {logo_err}")
            # --- End of Logo Addition Section ---


            # Save the image with logo for record (JPG format needs RGB)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            photo_path = os.path.join(PHOTOS_DIR, f"print_{timestamp}.jpg")
            try:
                # <<< CHANGE 4: Convert to RGB before saving as JPG >>>
                rgb_image_save = image_to_print.convert('RGB')
                rgb_image_save.save(photo_path, quality=95)
                print(f"Image saved for record: {photo_path}")
            except Exception as save_err:
                print(f"Warning: Could not save print copy: {save_err}")


            # Create temporary BMP file for printing (BMP usually needs RGB)
            temp_file = tempfile.NamedTemporaryFile(suffix='.bmp', delete=False)
            temp_path = temp_file.name
            temp_file.close() # Close the file handle so Pillow can write to the path

            # <<< CHANGE 5: Convert to RGB before saving as BMP >>>
            rgb_image_print_bmp = image_to_print.convert('RGB')
            rgb_image_print_bmp.save(temp_path, format='BMP')
            print(f"Using temporary file: {temp_path}")


            # Setup printer
            hDC = win32ui.CreateDC()
            hDC.CreatePrinterDC(printer_name)
            print("Printer DC created.")

            PHYSICALWIDTH = hDC.GetDeviceCaps(win32con.PHYSICALWIDTH)  # Use constant
            PHYSICALHEIGHT = hDC.GetDeviceCaps(win32con.PHYSICALHEIGHT) # Use constant
            printer_width_px = PHYSICALWIDTH
            printer_height_px = PHYSICALHEIGHT

            # Load the BMP for printing (already RGB)
            bmp = Image.open(temp_path)

            # Rotate if needed (adjust logic based on desired orientation vs paper size)
            # Consider if rotation is always needed or depends on aspect ratios
            print(f"Image size before rotation: {bmp.size}")
            print(f"Printer physical size: ({printer_width_px}, {printer_height_px})")

            # Landscape printing assumption: rotate if image is portrait (h > w)
            # This might need adjustment based on printer default orientation & paper
            bmp = bmp.rotate(90, expand=True)


            img_w, img_h = bmp.size
            print(f"Image size after potential rotation: {img_w}x{img_h}")

            # Scaling logic (Fit within printer page)
            img_aspect = img_w / img_h
            printer_aspect = printer_width_px / printer_height_px

            if img_aspect > printer_aspect:
                # Image is wider than printer page relative to height -> scale based on width
                scaled_w = printer_width_px
                scaled_h = int(scaled_w / img_aspect)
            else:
                # Image is taller than printer page relative to width -> scale based on height
                scaled_h = printer_height_px
                scaled_w = int(scaled_h * img_aspect)

            print(f"Scaled size for printing: {scaled_w}x{scaled_h}")

            # Centering
            x_offset = (printer_width_px - scaled_w) // 2
            y_offset = (printer_height_px - scaled_h) // 2
            print(f"Print offsets (X, Y): ({x_offset}, {y_offset})")

            # Print the image
            hDC.StartDoc(f"EdyBot Print {timestamp}")
            hDC.StartPage()
            dib = ImageWin.Dib(bmp) # Pass the potentially rotated BMP image
            # Draw onto the printer DC
            dib.draw(hDC.GetHandleOutput(), (x_offset, y_offset, x_offset + scaled_w, y_offset + scaled_h))
            hDC.EndPage()
            hDC.EndDoc()
            print("Print job sent.")

        except ImportError:
            print("ERROR: Missing required libraries (pywin32, Pillow). Please install them.")
        except Exception as e:
            import traceback
            print(f"ERROR during printing process: {e}")
            traceback.print_exc() # Print detailed traceback for debugging
        finally:
            if hDC:
                try:
                    hDC.DeleteDC()
                    print("Printer DC deleted.")
                except Exception as dc_err:
                    print(f"Error deleting DC: {dc_err}")
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                    print(f"Temporary file deleted: {temp_path}")
                except Exception as del_err:
                    print(f"Error deleting temporary file {temp_path}: {del_err}")


    # --- Chat Reset ---
    def reset_chat(self):
        """Resets chat display, attachment state, and Gemini session."""
        print("Resetting chat...")
        self._clear_attachment_state()
        self.image_sent_with_last_api_call = None
        self._chat_image_references.clear()

        # Destroy all existing bubble frames in the scrollable area
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        # Schedule scrolling to the top after the widgets are destroyed
        self.root.after(100, self._scroll_to_top)

        try:
            self.chat_session = create_gemini_chat(self.model_id, self.safety_settings)
            print("New Gemini chat session created.")
            self.root.after(200, self.load_initial_prompt)
        except Exception as e:
            print(f"ERROR: Failed to reset Gemini Chat Session: {e}")


    # --- Initial Prompt Load ---
    def load_initial_prompt(self):
        """Loads the initial prompt from file and sends it to the current session."""
        try:
            prompt_file = "initial_prompt.txt"
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding='utf-8') as f:
                    initial_prompt = f.read().strip()
                if initial_prompt:
                    print("Sending initial prompt...")
                    api_thread = threading.Thread(
                        target=self._send_to_gemini_thread_func,
                        args=([Part(text=initial_prompt)],),
                        daemon=True
                    )
                    api_thread.start()
                else:
                    print("System: initial_prompt.txt is empty.")
            else:
                print("System: initial_prompt.txt not found.")
        except Exception as e:
            print(f"System: Error loading initial prompt: {e}")

    # --- Closing ---
    def on_closing(self):
        """Handles the window close event."""
        print("Closing application...")
        self.camera_running = False
        self._chat_image_references.clear()
        if hasattr(self.camera_label, 'imgtk'):
            del self.camera_label.imgtk
        time.sleep(0.2)
        self.root.destroy()


# --- Main Execution ---
if __name__ == "__main__":
    if all(var in globals() for var in ['client', 'MODEL_ID', 'safety_settings', 'chat_session']):
        root = ctk.CTk()
        # root.state('zoomed')
        root.attributes('-fullscreen', True)
        app = MinimalChatApp(root, chat_session, client, MODEL_ID, safety_settings)
        root.mainloop()
    else:
        print("Critical Error: Global configuration or Gemini session failed during startup. Exiting.")
        error_root = ctk.CTk()
        error_root.withdraw()
        messagebox.showerror("Startup Error", "Gemini client/session failed. Check console for details.")
        error_root.destroy()
