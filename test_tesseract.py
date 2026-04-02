import pytesseract
from PIL import Image

try:
    text = pytesseract.image_to_string(Image.open('params_test.png'), lang='chi_sim')
    print("Extracted text:")
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    for i in range(0, len(lines), 2):
        if i+1 < len(lines):
            print(f"{lines[i]}：{lines[i+1]}")
        else:
            print(lines[i])
except Exception as e:
    print(e)
